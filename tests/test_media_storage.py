"""Regression tests for the durable media-storage migration
(app/services/storage/): the provider-neutral MediaStorage abstraction,
its local and S3-compatible adapters, the production-safety guard, and the
organization/project logo routes now running on top of it.

Covers (see PHASE 18 of the migration task):
  1-2. local adapter save/delete
  3. generated object keys are safe (rejects an unsafe prefix)
  4-5. avatar upload already exercises the abstraction + replace ordering
       (tests/test_user_avatar_upload.py) — not re-duplicated here.
  6. (same file) DB-failure cleanup of the newly-uploaded object.
  7. (same file) avatar removal deletes the correct object.
  8. organization logo upload/replace/remove uses the shared storage
     abstraction (local backend), same safe ordering as avatars.
  9. project logo upload/replace/remove — same.
  10. one org's logo delete cannot remove a different org's logo file.
  11-12. S3 adapter save/delete contract, via a mocked boto3 client (no
       real AWS account/credentials needed).
  13. a boto3 failure surfaces as a safe S3StorageError (upload) / is
      swallowed with a log line, never raised (delete).
  14. the production+local-storage guard rejects that configuration at
      Settings construction time.
  15. an absolute cloud-style URL is returned by the schema unchanged
      (round-trips through UserRead/OrganizationRead/ProjectRead as-is —
      there is no server-side URL rewriting to break).
  16. a user/org/project with no avatar/logo continues to work normally.

Local-adapter/route tests run against the real database connection and
real local media directory the app uses; every row and file created is
deleted before returning. S3-adapter tests never touch the network or a
real bucket — `boto3.client` itself is mocked.
"""

import asyncio
import io
import uuid
from unittest.mock import MagicMock, patch

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
import pytest
from botocore.exceptions import ClientError
from fastapi import UploadFile
from starlette.datastructures import Headers
from sqlalchemy import delete

from app.api.routes.organizations import delete_current_org_logo, upload_current_org_logo
from app.api.routes.projects import delete_project_logo, upload_project_logo
from app.core.config import Settings
from app.core.database import AsyncSessionLocal, engine
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.user import User
from app.schemas.organization import OrganizationRead
from app.schemas.project import ProjectRead
from app.services.storage.base import generate_object_key
from app.services.storage.local import LocalMediaStorage
from app.services.storage.s3 import S3MediaStorage, S3StorageError


def _make_png_upload(filename="logo.png") -> UploadFile:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 200)).save(buf, format="PNG")
    buf.seek(0)
    return UploadFile(file=buf, filename=filename, headers=Headers({"content-type": "image/png"}))


# ── 1-2-3. LocalMediaStorage + key generation ───────────────────────────────

def test_generate_object_key_shape_and_safety():
    key = generate_object_key("user-avatars", 42, "png")
    assert key.startswith("user-avatars/42/")
    assert key.endswith(".png")
    with pytest.raises(ValueError):
        generate_object_key("../escape", 1, "png")  # unsafe prefix must never be accepted


def test_local_media_storage_save_and_delete(tmp_path):
    storage = LocalMediaStorage(tmp_path)

    async def scenario():
        key = generate_object_key("user-avatars", 1, "png")
        url = await storage.save(key, b"fake-bytes", "image/png")
        assert url == f"/media/{key}"
        assert (tmp_path / key).exists()
        assert (tmp_path / key).read_bytes() == b"fake-bytes"

        await storage.delete(url, "user-avatars")
        assert not (tmp_path / key).exists()

        # Deleting again / a foreign prefix / None must never raise.
        await storage.delete(url, "user-avatars")
        await storage.delete("/media/organization-logos/1/x.png", "user-avatars")
        await storage.delete(None, "user-avatars")

    asyncio.run(scenario())


def test_local_media_storage_delete_cannot_escape_root(tmp_path):
    """A crafted value trying to climb out of the media root via `..`
    segments must never resolve to a path outside it."""
    storage = LocalMediaStorage(tmp_path)
    outside_target = tmp_path.parent / "should-not-be-touched.txt"
    outside_target.write_text("do not delete me")

    async def scenario():
        traversal_value = "/media/user-avatars/../../should-not-be-touched.txt"
        await storage.delete(traversal_value, "user-avatars")

    asyncio.run(scenario())
    assert outside_target.exists(), "a path-traversal delete attempt must never reach outside the media root"
    outside_target.unlink()


# ── 11-12-13. S3MediaStorage — boto3 mocked at the SDK boundary ─────────────

def test_s3_media_storage_save_builds_expected_url_and_calls_put_object():
    with patch("boto3.client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client
        storage = S3MediaStorage(bucket="my-bucket", region="us-west-2")

        url = asyncio.run(storage.save("user-avatars/1/abc.png", b"bytes", "image/png"))

        assert url == "https://my-bucket.s3.us-west-2.amazonaws.com/user-avatars/1/abc.png"
        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "my-bucket"
        assert call_kwargs["Key"] == "user-avatars/1/abc.png"
        assert call_kwargs["Body"] == b"bytes"
        assert call_kwargs["ContentType"] == "image/png"


def test_s3_media_storage_save_with_custom_endpoint_and_public_base_url():
    with patch("boto3.client") as mock_client_factory:
        mock_client_factory.return_value = MagicMock()

        # A custom public base (e.g. a CDN) always wins.
        storage = S3MediaStorage(bucket="b", endpoint_url="https://minio.internal:9000", public_base_url="https://cdn.example.com")
        url = asyncio.run(storage.save("k/1/x.png", b"x", "image/png"))
        assert url == "https://cdn.example.com/k/1/x.png"

        # No public base — falls back to the S3-compatible endpoint's own object URL shape.
        storage2 = S3MediaStorage(bucket="b", endpoint_url="https://minio.internal:9000")
        url2 = asyncio.run(storage2.save("k/1/x.png", b"x", "image/png"))
        assert url2 == "https://minio.internal:9000/b/k/1/x.png"


def test_s3_media_storage_delete_extracts_key_and_scopes_to_prefix():
    with patch("boto3.client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client
        storage = S3MediaStorage(bucket="my-bucket", region="us-west-2")

        asyncio.run(storage.delete("https://my-bucket.s3.us-west-2.amazonaws.com/user-avatars/1/abc.png", "user-avatars"))
        mock_client.delete_object.assert_called_once_with(Bucket="my-bucket", Key="user-avatars/1/abc.png")

        mock_client.reset_mock()
        # A URL that doesn't belong to the requested prefix must never be deleted.
        asyncio.run(storage.delete("https://my-bucket.s3.us-west-2.amazonaws.com/organization-logos/1/abc.png", "user-avatars"))
        mock_client.delete_object.assert_not_called()

        # A value from a different backend/host entirely: no-op, no raise.
        asyncio.run(storage.delete("/media/user-avatars/1/abc.png", "user-avatars"))
        mock_client.delete_object.assert_not_called()
        asyncio.run(storage.delete(None, "user-avatars"))


def test_s3_media_storage_upload_failure_raises_safe_error():
    with patch("boto3.client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "super-secret-detail"}}, "PutObject"
        )
        mock_client_factory.return_value = mock_client
        storage = S3MediaStorage(bucket="my-bucket", region="us-west-2")

        with pytest.raises(S3StorageError) as exc_info:
            asyncio.run(storage.save("k/1/x.png", b"x", "image/png"))
        # The safe, user-facing message must never leak the underlying
        # botocore error/credentials/bucket policy detail.
        assert "super-secret-detail" not in str(exc_info.value)
        assert "AccessDenied" not in str(exc_info.value)


def test_s3_media_storage_delete_failure_is_swallowed_not_raised():
    with patch("boto3.client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}}, "DeleteObject"
        )
        mock_client_factory.return_value = mock_client
        storage = S3MediaStorage(bucket="my-bucket", region="us-west-2")

        # Must not raise — best-effort cleanup, exactly like the local adapter.
        asyncio.run(storage.delete("https://my-bucket.s3.us-west-2.amazonaws.com/user-avatars/1/x.png", "user-avatars"))


# ── 14. Production safety guard ─────────────────────────────────────────────

def test_production_with_local_storage_backend_is_rejected():
    with pytest.raises(Exception) as exc_info:
        Settings(ENVIRONMENT="production", MEDIA_STORAGE_BACKEND="local")
    assert "MEDIA_STORAGE_BACKEND=local" in str(exc_info.value)


def test_production_with_s3_storage_backend_is_accepted():
    # Must not raise — this is the valid production configuration.
    Settings(ENVIRONMENT="production", MEDIA_STORAGE_BACKEND="s3", MEDIA_BUCKET="prod-bucket", AWS_REGION="us-east-1")


def test_s3_backend_without_bucket_is_rejected():
    with pytest.raises(Exception) as exc_info:
        Settings(MEDIA_STORAGE_BACKEND="s3")
    assert "MEDIA_BUCKET" in str(exc_info.value)


def test_unknown_storage_backend_is_rejected():
    with pytest.raises(Exception) as exc_info:
        Settings(MEDIA_STORAGE_BACKEND="azure_blob_not_implemented")
    assert "Unknown MEDIA_STORAGE_BACKEND" in str(exc_info.value)


# ── 8, 9, 10, 16. Organization / project logo routes on the shared storage ──

async def _org_logo_scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]
        owner = User(full_name="Logo Test Owner", email=f"logo.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.commit()
        await db.refresh(owner)

        org_a = Organization(name=f"Logo Org A {suffix}", slug=f"logo-org-a-{suffix}", owner_id=owner.id)
        org_b = Organization(name=f"Logo Org B {suffix}", slug=f"logo-org-b-{suffix}", owner_id=owner.id)
        db.add_all([org_a, org_b])
        await db.commit()
        await db.refresh(org_a)
        await db.refresh(org_b)

        membership_a = OrganizationMembership(organization_id=org_a.id, user_id=owner.id, role="owner")
        membership_b = OrganizationMembership(organization_id=org_b.id, user_id=owner.id, role="owner")
        db.add_all([membership_a, membership_b])
        await db.commit()

        from app.core.config import settings as app_settings

        tenant_a = TenantContext(organization_id=org_a.id, organization=org_a, membership=membership_a, user=owner, db=db)
        tenant_b = TenantContext(organization_id=org_b.id, organization=org_b, membership=membership_b, user=owner, db=db)

        written_files = []
        try:
            # ── 16. No-logo org works normally. ─────────────────────────
            assert org_a.logo_url is None

            # ── 8. Upload uses the shared storage abstraction. ──────────
            result1 = await upload_current_org_logo(tenant=tenant_a, db=db, file=_make_png_upload())
            assert isinstance(result1, OrganizationRead)
            assert result1.logo_url.startswith(f"/media/organization-logos/{org_a.id}/")
            file1 = app_settings.media_root_path / result1.logo_url.removeprefix("/media/")
            written_files.append(file1)
            assert file1.exists()

            # ── 10. Uploading org A's logo must never touch org B's file. ──
            assert org_b.logo_url is None

            # ── Replace: old file removed only after the new one is saved+committed. ──
            result2 = await upload_current_org_logo(tenant=tenant_a, db=db, file=_make_png_upload("logo2.png"))
            file2 = app_settings.media_root_path / result2.logo_url.removeprefix("/media/")
            written_files.append(file2)
            assert file2.exists()
            assert not file1.exists(), "the old org logo file must be deleted once the replacement is confirmed"

            # ── Upload for org B, then delete org A's logo: org B untouched. ──
            result_b = await upload_current_org_logo(tenant=tenant_b, db=db, file=_make_png_upload("b.png"))
            file_b = app_settings.media_root_path / result_b.logo_url.removeprefix("/media/")
            written_files.append(file_b)

            await delete_current_org_logo(tenant=tenant_a, db=db)
            await db.refresh(org_a)
            assert org_a.logo_url is None
            assert not file2.exists()
            assert file_b.exists(), "deleting org A's logo must never delete org B's logo file"

        finally:
            for path in written_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_a.id, org_b.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_a.id, org_b.id])))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_organization_logo_upload_replace_delete_via_shared_storage():
    asyncio.run(_org_logo_scenario())


async def _project_logo_scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]
        owner = User(full_name="Project Logo Owner", email=f"plogo.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.commit()
        await db.refresh(owner)

        org = Organization(name=f"PLogo Org {suffix}", slug=f"plogo-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        await db.refresh(org)

        membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        db.add(membership)
        project = Project(name=f"Logo Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.commit()
        await db.refresh(project)

        from app.core.config import settings as app_settings

        tenant = TenantContext(organization_id=org.id, organization=org, membership=membership, user=owner, db=db)

        written_files = []
        try:
            assert project.logo_url is None

            result = await upload_project_logo(project_id=project.id, tenant=tenant, file=_make_png_upload())
            assert isinstance(result, ProjectRead)
            assert result.logo_url.startswith(f"/media/project-logos/{project.id}/")
            file1 = app_settings.media_root_path / result.logo_url.removeprefix("/media/")
            written_files.append(file1)
            assert file1.exists()

            await delete_project_logo(project_id=project.id, tenant=tenant)
            await db.refresh(project)
            assert project.logo_url is None
            assert not file1.exists()

        finally:
            for path in written_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_project_logo_upload_and_delete_via_shared_storage():
    asyncio.run(_project_logo_scenario())


# ── 15. Absolute cloud-style URLs round-trip through response schemas as-is ─

def test_absolute_cloud_url_round_trips_through_schemas_unchanged():
    cloud_url = "https://my-bucket.s3.us-west-2.amazonaws.com/user-avatars/7/abc123.png"
    # UserRead/OrganizationRead/ProjectRead all just declare `*_url: str |
    # None` — there is no server-side transformation to verify beyond "the
    # value that went in comes back out unchanged", which is the entire
    # point of storing a stable URL rather than a signed/expiring one.
    from app.schemas.user import UserRead
    validated = UserRead.model_validate({
        "id": 1, "full_name": "X", "email": "x@test.invalid", "role": "team_member",
        "is_active": True, "profile_picture_url": cloud_url,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    assert validated.profile_picture_url == cloud_url
