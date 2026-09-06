from pathlib import Path

from app.services.storage.base import MediaStorage


class LocalMediaStorage(MediaStorage):
    """Development/test backend — writes under `settings.media_root_path`
    and returns the relative `/media/...` path the existing FastAPI
    `StaticFiles` mount already serves. This preserves the exact URL shape
    the app has always produced, so switching a fresh dev DB between
    `local` and a cloud backend never changes anything else."""

    def __init__(self, root: Path):
        self._root = root

    async def save(self, key: str, contents: bytes, content_type: str) -> str:
        del content_type  # local filesystem has no metadata to set
        destination = self._resolve(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
        return f"/media/{key}"

    async def delete(self, stored_value: str | None, prefix: str) -> None:
        if not stored_value or not stored_value.startswith(f"/media/{prefix}/"):
            # Not a local-storage value for this entity type at all (could
            # be a cloud URL from before a backend switch, or already gone)
            # — best-effort means doing nothing here is correct, not an error.
            return
        key = stored_value.removeprefix("/media/")
        try:
            path = self._resolve(key)
            if not self._is_within_root(path):
                return
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _resolve(self, key: str) -> Path:
        return self._root / key

    def _is_within_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self._root.resolve())
            return True
        except ValueError:
            return False
