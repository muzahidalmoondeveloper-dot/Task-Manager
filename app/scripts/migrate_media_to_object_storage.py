"""One-time, explicit, re-runnable migration of existing locally-stored
media (user avatars, organization logos, project logos) to whatever
durable MediaStorage backend is currently configured
(`MEDIA_STORAGE_BACKEND` — see app/services/storage/).

This never runs automatically. Run it by hand, once, when cutting a
deployment over from local disk to durable object storage:

    python -m app.scripts.migrate_media_to_object_storage --dry-run
    python -m app.scripts.migrate_media_to_object_storage

Safe to re-run: a row whose reference is already an absolute
`http(s)://` URL is treated as already migrated and skipped. A row whose
local file is missing is reported and left untouched (never a fatal
error for the rest of the run).

Ordering per row (never the reverse — see app/services/storage/base.py
and the avatar/logo upload routes for the same rule):
  1. confirm the local file referenced by the DB row actually exists
  2. upload its bytes to the configured durable backend
  3. only then update the DB reference to the new URL
  4. the local file is left on disk untouched unless
     --delete-local-after-migrate is passed explicitly

`--dry-run` performs steps 1 only (reports what *would* be migrated,
touches neither storage nor the database).
"""

import argparse
import asyncio
import logging

import main as _app_main  # noqa: F401 — registers every SQLAlchemy model/relationship before any query runs.
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.organization import Organization
from app.models.project import Project
from app.models.user import User
from app.services.storage import generate_object_key, get_media_storage
from app.services.storage.local import LocalMediaStorage

logger = logging.getLogger("app.scripts.migrate_media")

# (model, column name, DB-column attribute, storage prefix, content-type
# guess by extension — local files predate any stored Content-Type, so it's
# inferred from the filename this app itself generated).
_TARGETS = [
    (User, "profile_picture_url", "user-avatars"),
    (Organization, "logo_url", "organization-logos"),
    (Project, "logo_url", "project-logos"),
]

_CONTENT_TYPE_BY_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def _guess_content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream")


async def _migrate_row(db: AsyncSession, row, column: str, prefix: str, dry_run: bool, delete_local: bool, storage) -> str:
    """Returns one of: "migrated", "already-migrated", "missing-file", "failed"."""
    value = getattr(row, column)
    if not value:
        return "empty"

    if not value.startswith("/media/"):
        # Already an absolute cloud URL (or something else entirely) —
        # nothing local to migrate.
        return "already-migrated"

    local_relative = value.removeprefix("/media/")
    local_path = settings.media_root_path / local_relative
    if not local_path.exists():
        logger.warning("Missing local file for %s.%s=%s (id=%s): %s", type(row).__name__, column, value, row.id, local_path)
        return "missing-file"

    if dry_run:
        logger.info("[dry-run] would migrate %s.%s (id=%s): %s -> %s/...", type(row).__name__, column, row.id, value, prefix)
        return "migrated"

    contents = local_path.read_bytes()
    extension = local_path.suffix.lstrip(".") or "png"
    key = generate_object_key(prefix, row.id, extension)
    content_type = _guess_content_type(local_path.name)

    try:
        new_url = await storage.save(key, contents, content_type)
    except Exception:
        logger.exception("Upload failed for %s.%s (id=%s), local file left untouched", type(row).__name__, column, row.id)
        return "failed"

    # Step 3: only now update the DB reference — the upload above already
    # succeeded (an exception would have returned "failed" first).
    setattr(row, column, new_url)
    await db.commit()
    logger.info("Migrated %s.%s (id=%s): %s -> %s", type(row).__name__, column, row.id, value, new_url)

    if delete_local:
        try:
            local_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete local source %s after successful migration (safe to ignore/cleanup later)", local_path)

    return "migrated"


async def run(dry_run: bool, delete_local: bool) -> dict:
    storage = get_media_storage()
    if dry_run and isinstance(storage, LocalMediaStorage):
        logger.info("Dry-run against the local backend — this only reports what a real run would find; nothing is ever destructive in dry-run mode regardless of backend.")
    elif isinstance(storage, LocalMediaStorage):
        logger.warning(
            "MEDIA_STORAGE_BACKEND is still 'local' — there is no durable target to migrate to. "
            "Set MEDIA_STORAGE_BACKEND=s3 (and MEDIA_BUCKET/AWS_REGION) before running this for real."
        )

    counts: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        for model, column, prefix in _TARGETS:
            result = await db.execute(select(model))
            rows = result.scalars().all()
            for row in rows:
                outcome = await _migrate_row(db, row, column, prefix, dry_run, delete_local, storage)
                counts[outcome] = counts.get(outcome, 0) + 1

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report what would be migrated without uploading or touching the database.")
    parser.add_argument(
        "--delete-local-after-migrate",
        action="store_true",
        help="Delete each local file only after its upload + DB update both succeed. Off by default — local originals are left in place for a manual/later cleanup pass.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    counts = asyncio.run(run(dry_run=args.dry_run, delete_local=args.delete_local_after_migrate))

    print("\n--- Media migration summary ---")
    for outcome in ("migrated", "already-migrated", "missing-file", "failed", "empty"):
        if outcome in counts:
            print(f"{outcome:>18}: {counts[outcome]}")
    if args.dry_run:
        print("\n(dry run — nothing was uploaded or changed; re-run without --dry-run to apply)")
    if counts.get("failed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
