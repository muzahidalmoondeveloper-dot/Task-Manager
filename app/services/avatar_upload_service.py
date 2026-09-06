import io

from fastapi import UploadFile
from fastapi import status as http_status
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.auth_errors import AppException, ErrorDef
from app.services.storage import generate_object_key, get_media_storage

_INVALID_TYPE = ErrorDef(
    code="AVATAR_INVALID_TYPE",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Profile picture must be a PNG, JPEG, or WEBP image.",
)
_TOO_LARGE = ErrorDef(
    code="AVATAR_TOO_LARGE",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Profile picture must be smaller than 5 MB.",
)
_CORRUPT = ErrorDef(
    code="AVATAR_INVALID_IMAGE",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="This file isn't a valid image. Please choose a different picture.",
)

# Same allowlist/limit as app/services/logo_upload_service.py, kept as its
# own copy rather than imported — this module additionally decodes the
# image with Pillow (real content, not just the declared Content-Type),
# which the logo service deliberately doesn't do, so the two aren't
# actually sharing validation logic under the hood even where the numbers
# match.
_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_MAX_BYTES = 5 * 1024 * 1024
_MAX_DIMENSION = 512  # avatars are always displayed small; no reason to keep a 4000px original
_AVATAR_PREFIX = "user-avatars"


def _normalize(contents: bytes, extension: str) -> tuple[bytes, str]:
    """Decode, auto-orient, and downscale an uploaded avatar. Raises
    AppException(_CORRUPT) if the bytes aren't actually a decodable image,
    regardless of what Content-Type/extension the client claimed."""
    try:
        image = Image.open(io.BytesIO(contents))
        image.verify()  # cheap structural check; re-open below to actually decode/process
        image = Image.open(io.BytesIO(contents))
        image = ImageOps.exif_transpose(image)  # respect EXIF orientation before it's stripped
    except (UnidentifiedImageError, OSError, ValueError):
        raise AppException(_CORRUPT)

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

    if image.width > _MAX_DIMENSION or image.height > _MAX_DIMENSION:
        image.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION), Image.LANCZOS)

    buffer = io.BytesIO()
    if extension == "png" and image.mode == "RGBA":
        image.save(buffer, format="PNG", optimize=True)
    else:
        # JPEG/WEBP output has no alpha channel; flatten onto white first so
        # a transparent PNG re-encoded as jpg doesn't turn black.
        if image.mode == "RGBA":
            flattened = Image.new("RGB", image.size, (255, 255, 255))
            flattened.paste(image, mask=image.split()[3])
            image = flattened
        elif image.mode != "RGB":
            image = image.convert("RGB")
        if extension == "webp":
            image.save(buffer, format="WEBP", quality=85)
        else:
            image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), extension


async def save_avatar(file: UploadFile, user_id: int) -> str:
    """Validate, normalize, and persist an uploaded avatar via the
    configured MediaStorage backend (local disk in dev/test, durable
    object storage in production — see app/services/storage/), returning
    its stable, servable URL. Mirrors logo_upload_service.save_logo's
    contract but additionally decodes the image content with Pillow
    instead of trusting the declared Content-Type, and downsizes oversized
    originals. This module owns image validation/normalization only — it
    has no idea where or how bytes are actually stored."""
    extension = _CONTENT_TYPES.get(file.content_type)
    if extension is None:
        raise AppException(_INVALID_TYPE)

    contents = await file.read()
    if len(contents) > _MAX_BYTES:
        raise AppException(_TOO_LARGE)

    normalized, extension = _normalize(contents, extension)

    key = generate_object_key(_AVATAR_PREFIX, user_id, extension)
    return await get_media_storage().save(key, normalized, file.content_type)


AVATAR_PREFIX = _AVATAR_PREFIX
