import uuid
from pathlib import Path

from fastapi import UploadFile
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.config import settings

_INVALID_TYPE = ErrorDef(code="DOCUMENT_INVALID_TYPE", status=http_status.HTTP_400_BAD_REQUEST, message="File type is not allowed for this document.")
_TOO_LARGE = ErrorDef(code="DOCUMENT_TOO_LARGE", status=http_status.HTTP_400_BAD_REQUEST, message="File exceeds the maximum allowed size.")

# Extension -> allowed content-types. Broad enough for the document types the
# workflow spec calls out (briefs, contracts, brand assets, technical docs).
_EXTENSION_CONTENT_TYPES: dict[str, set[str]] = {
    "pdf": {"application/pdf"},
    "doc": {"application/msword"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "xls": {"application/vnd.ms-excel"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "ppt": {"application/vnd.ms-powerpoint"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "png": {"image/png"},
    "jpg": {"image/jpeg"},
    "jpeg": {"image/jpeg"},
    "webp": {"image/webp"},
    "txt": {"text/plain"},
    "csv": {"text/csv", "application/vnd.ms-excel"},
}
_ALL_ALLOWED_EXTENSIONS = set(_EXTENSION_CONTENT_TYPES.keys())
_DEFAULT_MAX_MB = 10
_HARD_CAP_MB = 100  # never allow above this regardless of requirement config


def _extension_from_filename(filename: str) -> str:
    return Path(filename).suffix.lstrip(".").lower()


async def save_document(
    file: UploadFile,
    subdir: str,
    entity_id,
    allowed_file_types: str | None = None,
    max_file_size_mb: int | None = None,
) -> tuple[str, str, int]:
    """Validate and persist an uploaded document, returning
    (servable_url, extension, byte_size). `allowed_file_types` is an optional
    comma-separated allow-list (e.g. "pdf,docx"); defaults to the full
    supported set when not given by the requirement."""
    extension = _extension_from_filename(file.filename or "")
    allowed = _ALL_ALLOWED_EXTENSIONS
    if allowed_file_types:
        requested = {t.strip().lower().lstrip(".") for t in allowed_file_types.split(",") if t.strip()}
        allowed = requested & _ALL_ALLOWED_EXTENSIONS or requested

    if not extension or extension not in allowed:
        raise AppException(_INVALID_TYPE)

    expected_content_types = _EXTENSION_CONTENT_TYPES.get(extension)
    if expected_content_types and file.content_type not in expected_content_types:
        raise AppException(_INVALID_TYPE)

    max_mb = min(max_file_size_mb or _DEFAULT_MAX_MB, _HARD_CAP_MB)
    max_bytes = max_mb * 1024 * 1024

    contents = await file.read()
    if len(contents) > max_bytes:
        raise AppException(_TOO_LARGE)

    doc_dir = settings.media_root_path / subdir
    doc_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{entity_id}-{uuid.uuid4().hex}.{extension}"
    (doc_dir / filename).write_bytes(contents)
    return f"/media/{subdir}/{filename}", extension, len(contents)


def delete_document_file(file_url: str | None, subdir: str) -> None:
    """Best-effort cleanup — never let a missing/already-deleted file block the request."""
    if not file_url or not file_url.startswith(f"/media/{subdir}/"):
        return
    file_path = settings.media_root_path / subdir / Path(file_url).name
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        pass
