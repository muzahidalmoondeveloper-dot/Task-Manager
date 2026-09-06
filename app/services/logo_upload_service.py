from fastapi import UploadFile
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.services.storage import generate_object_key, get_media_storage

_INVALID_TYPE = ErrorDef(code="LOGO_INVALID_TYPE", status=http_status.HTTP_400_BAD_REQUEST, message="Logo must be a PNG, JPEG, or WEBP image.")
_TOO_LARGE = ErrorDef(code="LOGO_TOO_LARGE", status=http_status.HTTP_400_BAD_REQUEST, message="Logo must be smaller than 5 MB.")

_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_MAX_BYTES = 5 * 1024 * 1024


async def save_logo(file: UploadFile, prefix: str, entity_id) -> str:
    """Validate and persist an uploaded logo image via the configured
    MediaStorage backend (local disk in dev/test, durable object storage in
    production — app/services/storage/), returning its stable, servable
    URL. `prefix` scopes storage per entity type (e.g. "project-logos",
    "organization-logos") so cleanup never crosses types."""
    extension = _CONTENT_TYPES.get(file.content_type)
    if extension is None:
        raise AppException(_INVALID_TYPE)

    contents = await file.read()
    if len(contents) > _MAX_BYTES:
        raise AppException(_TOO_LARGE)

    key = generate_object_key(prefix, entity_id, extension)
    return await get_media_storage().save(key, contents, file.content_type)


async def delete_logo_file(logo_url: str | None, prefix: str) -> None:
    """Best-effort cleanup of the old logo file when it's replaced/removed —
    never let a missing/already-deleted file, or a cloud-provider hiccup,
    block the request."""
    await get_media_storage().delete(logo_url, prefix)
