import uuid
from pathlib import Path

from fastapi import UploadFile
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.config import settings

_INVALID_TYPE = ErrorDef(code="LOGO_INVALID_TYPE", status=http_status.HTTP_400_BAD_REQUEST, message="Logo must be a PNG, JPEG, or WEBP image.")
_TOO_LARGE = ErrorDef(code="LOGO_TOO_LARGE", status=http_status.HTTP_400_BAD_REQUEST, message="Logo must be smaller than 5 MB.")

_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_MAX_BYTES = 5 * 1024 * 1024


async def save_logo(file: UploadFile, subdir: str, entity_id) -> str:
    """Validate and persist an uploaded logo image, returning its servable
    `/media/...` URL. `subdir` scopes storage per entity type (e.g.
    "project_logos", "organization_logos") so cleanup never crosses types."""
    extension = _CONTENT_TYPES.get(file.content_type)
    if extension is None:
        raise AppException(_INVALID_TYPE)

    contents = await file.read()
    if len(contents) > _MAX_BYTES:
        raise AppException(_TOO_LARGE)

    logo_dir = settings.media_root_path / subdir
    logo_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{entity_id}-{uuid.uuid4().hex}.{extension}"
    (logo_dir / filename).write_bytes(contents)
    return f"/media/{subdir}/{filename}"


def delete_logo_file(logo_url: str | None, subdir: str) -> None:
    """Best-effort cleanup of the old logo file when it's replaced/removed —
    never let a missing/already-deleted file block the request."""
    if not logo_url or not logo_url.startswith(f"/media/{subdir}/"):
        return
    file_path = settings.media_root_path / subdir / Path(logo_url).name
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        pass
