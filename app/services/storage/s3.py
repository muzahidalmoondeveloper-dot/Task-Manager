"""S3-compatible durable storage adapter.

Chosen as the reference durable backend because no cloud provider is
established anywhere in this repository (no Dockerfile/CI/deployment
config, no Azure/AWS SDK, no cloud env vars beyond OAuth client ids —
see the migration task's final report for the full audit). S3's API is
implemented not just by AWS but by most other object-storage providers
(Cloudflare R2, DigitalOcean Spaces, MinIO, Backblaze B2, ...), so this
adapter works with any of them via `AWS_S3_ENDPOINT_URL` without locking
the app into one vendor — the actual provider is still a deployment
decision the team must make (see MEDIA_STORAGE.md).

boto3 is a synchronous SDK; every call is offloaded to a worker thread via
`asyncio.to_thread` so it never blocks the event loop.
"""

import asyncio
import logging

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.services.storage.base import MediaStorage

logger = logging.getLogger("app.storage.s3")


class S3StorageError(Exception):
    """Raised when a cloud storage operation fails. The message is always
    safe to surface to a caller — it never includes credentials, the
    underlying botocore exception, or a stack trace."""


class S3MediaStorage(MediaStorage):
    def __init__(
        self,
        *,
        bucket: str,
        region: str | None = None,
        endpoint_url: str | None = None,
        public_base_url: str | None = None,
    ):
        if not bucket:
            raise ValueError("S3MediaStorage requires a bucket name (MEDIA_BUCKET).")
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        # Optional CDN/custom-domain override (e.g. a CloudFront distribution
        # or R2 custom domain in front of the bucket) — falls back to the
        # provider's own object URL shape when unset.
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        # Credentials are never read from settings/config directly here —
        # boto3's default credential chain (environment, shared config,
        # instance/task role, workload identity) supplies them, matching
        # "prefer managed identity / standard credential chain" (Phase 4).
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )

    async def save(self, key: str, contents: bytes, content_type: str) -> str:
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=contents,
                ContentType=content_type,
                # Public-read matches this app's existing behavior: avatars/
                # logos are already served with no auth check at all via the
                # local `/media` StaticFiles mount (see MEDIA_STORAGE.md for
                # the explicit public-vs-private decision).
                ACL="public-read",
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("S3 upload failed for key=%s bucket=%s: %s", key, self._bucket, type(exc).__name__)
            raise S3StorageError("Unable to store the uploaded file right now. Please try again.") from exc

        return self._public_url(key)

    async def delete(self, stored_value: str | None, prefix: str) -> None:
        key = self._extract_key(stored_value, prefix)
        if key is None:
            return
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            # Best-effort: an orphaned object is a monitoring/cleanup
            # concern, never a reason to fail the caller's already-
            # successful DB update.
            logger.warning("S3 delete failed for key=%s bucket=%s: %s", key, self._bucket, type(exc).__name__)

    def _public_url(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}/{key}"
        if self._endpoint_url:
            return f"{self._endpoint_url.rstrip('/')}/{self._bucket}/{key}"
        region_segment = f".{self._region}" if self._region and self._region != "us-east-1" else ""
        return f"https://{self._bucket}.s3{region_segment}.amazonaws.com/{key}"

    def _extract_key(self, stored_value: str | None, prefix: str) -> str | None:
        """Recover the object key from a previously-returned public URL,
        scoped to `prefix` — never trusts an arbitrary key computed from
        `stored_value` beyond stripping our own known base URL, and refuses
        anything that doesn't land inside `prefix` (defense in depth: even
        if `stored_value` were somehow attacker-influenced, this can only
        ever resolve to a key under the caller's own entity-type prefix)."""
        if not stored_value:
            return None
        for base in filter(None, (self._public_base_url, self._endpoint_url and f"{self._endpoint_url.rstrip('/')}/{self._bucket}")):
            if stored_value.startswith(f"{base}/"):
                key = stored_value.removeprefix(f"{base}/")
                return key if key.startswith(f"{prefix}/") else None
        expected_hosts = (
            f"https://{self._bucket}.s3.amazonaws.com/",
            f"https://{self._bucket}.s3.{self._region}.amazonaws.com/" if self._region else None,
        )
        for host in filter(None, expected_hosts):
            if stored_value.startswith(host):
                key = stored_value.removeprefix(host)
                return key if key.startswith(f"{prefix}/") else None
        return None
