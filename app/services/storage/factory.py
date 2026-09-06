from app.core.config import settings
from app.services.storage.base import MediaStorage
from app.services.storage.local import LocalMediaStorage

_instance: MediaStorage | None = None


def get_media_storage() -> MediaStorage:
    """Returns the process-wide MediaStorage adapter selected by
    `settings.MEDIA_STORAGE_BACKEND`. Cached after first call — the
    backend is fixed for the lifetime of the process, same as
    `app.core.redis_client.get_redis()`'s singleton pattern.

    Settings itself already refuses to start with an unsafe combination
    (production + local — see Settings._guard_production_media_storage),
    so by the time this runs the configuration is known-valid.
    """
    global _instance
    if _instance is not None:
        return _instance

    if settings.MEDIA_STORAGE_BACKEND == "local":
        _instance = LocalMediaStorage(settings.media_root_path)
    elif settings.MEDIA_STORAGE_BACKEND == "s3":
        # Imported lazily so `boto3` is only required when the s3 backend
        # is actually selected — local dev/test never needs it importable.
        from app.services.storage.s3 import S3MediaStorage

        _instance = S3MediaStorage(
            bucket=settings.MEDIA_BUCKET,
            region=settings.AWS_REGION,
            endpoint_url=settings.AWS_S3_ENDPOINT_URL,
            public_base_url=settings.MEDIA_PUBLIC_BASE_URL,
        )
    else:
        # Unreachable in practice — Settings validation already rejects
        # this — but fail loudly rather than silently defaulting if it
        # ever is (e.g. settings mutated in a test).
        raise RuntimeError(f"Unknown MEDIA_STORAGE_BACKEND: {settings.MEDIA_STORAGE_BACKEND!r}")

    return _instance


def reset_media_storage_for_tests() -> None:
    """Test-only: drop the cached singleton so a test that mutates
    settings.MEDIA_STORAGE_BACKEND gets a fresh adapter instead of the
    previous test's cached one."""
    global _instance
    _instance = None
