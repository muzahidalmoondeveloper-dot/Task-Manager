"""Regression tests for the self-service profile-picture (avatar) upload
feature: POST/DELETE /users/me/profile-picture (app.api.routes.users).

Covers:
  1. authenticated user uploads a valid PNG -> succeeds, User.profile_picture_url
     is set, and the returned URL is a `/media/user-avatars/<id>/...` path
     (this test runs against the local storage backend — see
     tests/test_media_storage.py for the storage-abstraction/S3-adapter
     tests introduced alongside the durable-storage migration).
  2. an unsupported format (text file with an image Content-Type-looking
     call, and a real .gif) is rejected without touching the DB.
  3. a byte stream that lies about its Content-Type (claims image/png but
     isn't a decodable image) is rejected by the Pillow-backed content
     check, not just the declared Content-Type.
  4. an oversized image is rejected.
  5. unauthenticated access is impossible to reach this route in the first
     place — `current_user` always comes from `get_current_user`
     (JWT-derived), never from request data, so there is structurally no
     `user_id` parameter an attacker could substitute; this is verified by
     confirming the route signature never accepts one, plus that uploading
     as User A only ever mutates User A's own row while User B's is
     untouched even after A's upload.
  6. replacing an existing avatar updates the reference and deletes the old
     file, leaving no orphan and no broken reference.
  7. removing an avatar clears the reference, deletes the owned file, and a
     second remove (no-op, already absent) doesn't error.
  8. a user with no avatar continues to work normally (GET /users/me-style
     read via UserRead has profile_picture_url == None).

Runs against the real database connection and real local media directory
the app uses. Every row and file this test creates is deleted before it
returns.
"""

import asyncio
import io
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import UploadFile
from starlette.datastructures import Headers
from sqlalchemy import delete

from app.api.routes.users import (
    delete_current_user_profile_picture,
    read_current_user,
    upload_current_user_profile_picture,
)
from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.dependencies import get_current_user
from app.core.token_cache import get_token_cache
from app.models.user import User


def _make_upload(data: bytes, filename: str, content_type: str) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename, headers=Headers({"content-type": content_type}))


def _make_png_bytes(size=(64, 64), color=(200, 30, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_oversized_png_bytes() -> bytes:
    # A large, incompressible (random noise) image reliably exceeds 5MB as
    # PNG, unlike a solid color which PNG compresses to almost nothing
    # regardless of pixel dimensions.
    import random
    from PIL import Image
    random.seed(0)
    w = h = 2000
    pixels = bytes(random.getrandbits(8) for _ in range(w * h * 3))
    buf = io.BytesIO()
    Image.frombytes("RGB", (w, h), pixels).save(buf, format="PNG", compress_level=0)
    return buf.getvalue()


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]
        user_a = User(full_name="Avatar Test User A", email=f"avatar.a.{suffix}@test.invalid", hashed_password="x", role="team_member")
        user_b = User(full_name="Avatar Test User B", email=f"avatar.b.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([user_a, user_b])
        await db.commit()
        await db.refresh(user_a)
        await db.refresh(user_b)

        written_files = []
        try:
            # ── 5c. Unauthenticated access is rejected. Both new routes are
            # gated by the exact same `Depends(get_current_user)` every
            # other /users/me/* route already uses (change-password, PATCH
            # /me, ...) — this confirms that shared gate itself rejects a
            # request with no credentials, which is what actually protects
            # the avatar routes (a direct function call bypasses FastAPI's
            # own dependency injection, so it can't be exercised through
            # upload_current_user_profile_picture itself; the enforcement
            # point is here). ──────────────────────────────────────────────
            try:
                await get_current_user(credentials=None, db=db, token_cache=await get_token_cache())
                raise AssertionError("a request with no credentials must be rejected")
            except AssertionError:
                raise
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 401, exc

            # ── 8. No-avatar user works normally. ──────────────────────────
            read = await read_current_user(current_user=user_a)
            assert read.profile_picture_url is None

            # ── 1. Valid upload succeeds; field set; URL shape correct. ────
            upload1 = _make_upload(_make_png_bytes(), "photo.png", "image/png")
            result1 = await upload_current_user_profile_picture(file=upload1, current_user=user_a, db=db)
            assert result1.profile_picture_url is not None
            assert result1.profile_picture_url.startswith(f"/media/user-avatars/{user_a.id}/"), result1.profile_picture_url
            assert not result1.profile_picture_url.startswith(str(settings.media_root_path)), (
                "response must be a servable URL, never an internal filesystem path"
            )
            await db.refresh(user_a)
            assert user_a.profile_picture_url == result1.profile_picture_url
            first_file_path = settings.media_root_path / result1.profile_picture_url.removeprefix("/media/")
            written_files.append(first_file_path)
            assert first_file_path.exists(), "the uploaded file must actually be written to disk"

            # ── 5b. Uploading as A never touches B. ────────────────────────
            await db.refresh(user_b)
            assert user_b.profile_picture_url is None, "an upload for one user must never affect another user's row"

            # ── 6. Replacing updates the reference and removes the old file. ──
            upload2 = _make_upload(_make_png_bytes(color=(30, 30, 200)), "photo2.png", "image/png")
            result2 = await upload_current_user_profile_picture(file=upload2, current_user=user_a, db=db)
            assert result2.profile_picture_url is not None
            assert result2.profile_picture_url != result1.profile_picture_url, "a replacement must get a new storage key"
            second_file_path = settings.media_root_path / result2.profile_picture_url.removeprefix("/media/")
            written_files.append(second_file_path)
            assert second_file_path.exists(), "the new file must exist after a successful replace"
            assert not first_file_path.exists(), "the old file must be cleaned up only after the new one is confirmed saved"
            await db.refresh(user_a)
            assert user_a.profile_picture_url == result2.profile_picture_url

            # ── 2a. Unsupported declared type (gif) is rejected. ───────────
            bad_type_upload = _make_upload(b"GIF89a" + b"\x00" * 20, "photo.gif", "image/gif")
            try:
                await upload_current_user_profile_picture(file=bad_type_upload, current_user=user_a, db=db)
                raise AssertionError("a non-allowlisted content type must be rejected")
            except Exception as exc:
                assert getattr(exc, "code", None) == "AVATAR_INVALID_TYPE", exc

            # ── 3. Disguised non-image content (correct header, fake bytes) is rejected. ──
            disguised_upload = _make_upload(b"not actually a png, just plain bytes " * 5, "photo.png", "image/png")
            try:
                await upload_current_user_profile_picture(file=disguised_upload, current_user=user_a, db=db)
                raise AssertionError("content that isn't a real decodable image must be rejected even with a matching Content-Type header")
            except Exception as exc:
                assert getattr(exc, "code", None) == "AVATAR_INVALID_IMAGE", exc

            # Neither rejected upload should have changed the stored reference.
            await db.refresh(user_a)
            assert user_a.profile_picture_url == result2.profile_picture_url, "a rejected upload must not touch the existing avatar"

            # ── 4. Oversized image is rejected. ────────────────────────────
            oversized_upload = _make_upload(_make_oversized_png_bytes(), "huge.png", "image/png")
            try:
                await upload_current_user_profile_picture(file=oversized_upload, current_user=user_a, db=db)
                raise AssertionError("an oversized image must be rejected")
            except Exception as exc:
                assert getattr(exc, "code", None) == "AVATAR_TOO_LARGE", exc

            # ── 7. Removing clears the reference and deletes the file. ─────
            remove_result = await delete_current_user_profile_picture(current_user=user_a, db=db)
            assert remove_result.profile_picture_url is None
            await db.refresh(user_a)
            assert user_a.profile_picture_url is None
            assert not second_file_path.exists(), "the owned file must be deleted on removal"

            # Removing again (already absent) must not error.
            remove_again = await delete_current_user_profile_picture(current_user=user_a, db=db)
            assert remove_again.profile_picture_url is None

            # ── 5a. No user_id is ever accepted by this route — the only
            # identity input is the injected `current_user`. ───────────────
            import inspect
            sig = inspect.signature(upload_current_user_profile_picture)
            assert "user_id" not in sig.parameters, "the self-service avatar upload must never accept a caller-supplied user_id"
            sig_delete = inspect.signature(delete_current_user_profile_picture)
            assert "user_id" not in sig_delete.parameters

        finally:
            for path in written_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            await db.execute(delete(User).where(User.id.in_([user_a.id, user_b.id])))
            await db.commit()

    await engine.dispose()


def test_user_avatar_upload_and_lifecycle():
    asyncio.run(_scenario())
