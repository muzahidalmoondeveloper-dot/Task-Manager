"""Provider-neutral media storage interface.

Everything outside this package should talk to media storage only through
`MediaStorage` (obtained via `app.services.storage.factory.get_media_storage`).
No route, repository, or upload service should import boto3/azure SDKs,
construct cloud URLs, or write to `Path(...)` directly — that knowledge is
confined to the adapters in this package.

Responsibility split (see avatar_upload_service.py / logo_upload_service.py):
  - the *upload service* owns validation/normalization of the image itself
    (allowed types, size limit, Pillow decode/EXIF/resize) and decides the
    logical object key.
  - the *storage adapter* only knows WHERE/HOW bytes are persisted and
    returns the stable, servable reference to store in the DB.
"""

import re
import uuid
from abc import ABC, abstractmethod

# Object keys are always `<prefix>/<entity_id>/<uuid>.<ext>` — never a raw
# client-supplied filename, never guessable/collidable across entities. The
# same key shape is used for local and cloud backends so switching backends
# never requires reshaping how keys look.
_SAFE_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def generate_object_key(prefix: str, entity_id: object, extension: str) -> str:
    """Build a safe, collision-free object key for a newly uploaded file.

    `prefix` must be one of this app's own fixed constants (e.g.
    "user-avatars") — never derived from user input — so this assert is a
    programmer-error guard, not input validation.
    """
    if not _SAFE_PREFIX_RE.match(prefix):
        raise ValueError(f"Unsafe storage prefix: {prefix!r}")
    extension = extension.lstrip(".").lower()
    return f"{prefix}/{entity_id}/{uuid.uuid4().hex}.{extension}"


class MediaStorage(ABC):
    """A durable place to persist small uploaded media (avatars, logos, ...)
    and get a stable, servable reference back."""

    @abstractmethod
    async def save(self, key: str, contents: bytes, content_type: str) -> str:
        """Persist `contents` under `key`. Returns the value to store in the
        DB reference column (`profile_picture_url`, `logo_url`, ...) — a
        relative `/media/...` path for local storage, or a stable public
        object URL for cloud storage. Never a temporary/expiring URL."""

    @abstractmethod
    async def delete(self, stored_value: str | None, prefix: str) -> None:
        """Best-effort delete of a previously-saved object, given exactly
        the value that was stored in the DB column. Must never raise —
        callers rely on this for safe cleanup after a successful replace/
        remove, and a missing/already-deleted/foreign-backend value is not
        an error. `prefix` scopes the delete to one entity type (e.g.
        "user-avatars") so this can never be tricked into deleting an
        object outside that prefix, even if `stored_value` were somehow
        attacker-influenced.

        Declared async (even though the local adapter's implementation is
        a plain synchronous unlink) so the cloud adapter's genuinely
        network-bound delete never blocks the event loop, and callers
        don't need to know which backend is active."""
