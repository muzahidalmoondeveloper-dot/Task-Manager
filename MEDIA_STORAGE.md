# Media Storage (avatars, organization/project logos)

## Background

User avatars and organization/project logos used to be written straight to
the local filesystem under `media/`. That's fine for local development and
the automated test suite, but it is **not durable in production**: a
restart, redeploy, instance replacement, horizontal scale-out, or container
recreation on most container/PaaS platforms can lose those files or make
them inconsistent between instances (instance A serves an avatar instance B
never received).

This app now goes through a small storage abstraction
(`app/services/storage/`) with two adapters:

- **`local`** (`app/services/storage/local.py`) — the historical behavior.
  Dev/test only.
- **`s3`** (`app/services/storage/s3.py`) — durable, S3-compatible object
  storage. Works with real AWS S3 or any S3-compatible provider (Cloudflare
  R2, DigitalOcean Spaces, MinIO, Backblaze B2, ...) via
  `AWS_S3_ENDPOINT_URL`.

**No cloud provider is otherwise established anywhere in this
repository** — there is no Dockerfile, CI/CD config, deployment script, or
existing Azure/AWS SDK usage to infer a decision from. S3-compatible was
chosen as the reference durable adapter specifically because it is not a
single-vendor commitment: it's implemented by AWS and by most competing
object-storage providers, so picking an actual provider is still a
deployment decision your team makes by setting `AWS_S3_ENDPOINT_URL` (or
leaving it unset for real AWS S3) — the application code doesn't need to
change either way.

## Configuration

| Variable | Required when | Purpose |
|---|---|---|
| `ENVIRONMENT` | always (defaults to `development`) | `development` \| `test` \| `production`. Gates the safety check below. |
| `MEDIA_STORAGE_BACKEND` | always (defaults to `local`) | `local` or `s3`. |
| `MEDIA_BUCKET` | `MEDIA_STORAGE_BACKEND=s3` | Target bucket name. |
| `AWS_REGION` | recommended for `s3` | Bucket region (used to build the public object URL). |
| `AWS_S3_ENDPOINT_URL` | non-AWS S3-compatible provider only | e.g. your R2/MinIO/Spaces endpoint. Leave unset for real AWS S3. |
| `MEDIA_PUBLIC_BASE_URL` | optional | CDN/custom domain in front of the bucket (e.g. a CloudFront distribution). Overrides the default object-URL shape when set. |

AWS credentials are **not** application settings. `S3MediaStorage` calls
plain `boto3.client("s3", ...)`, which uses boto3's own standard credential
chain: environment variables, a shared `~/.aws/credentials` profile, or —
preferred in production — an EC2/ECS/EKS instance or task role (workload
identity). Nothing secret ever passes through this app's `Settings` object,
gets logged, or reaches the frontend.

## Production safety guard

`Settings` refuses to construct — the app fails at startup, not on first
upload — if `ENVIRONMENT=production` and `MEDIA_STORAGE_BACKEND=local`.
There is no silent fallback to local disk on a cloud failure or
misconfiguration; that would produce data inconsistent across instances.

## Public vs. private media

Avatars and logos are treated as **public-read** objects. This matches the
app's existing (pre-migration) behavior exactly: the local `/media` mount
is a plain `StaticFiles` mount with no authentication or authorization
check at all today, so anyone with a URL can already view any avatar/logo.
Moving to S3 with `ACL: public-read` preserves that behavior rather than
changing it. If stricter privacy is wanted later, that's a deliberate
follow-up decision (server-generated, short-lived signed URLs) — out of
scope for this storage migration, which is meant to fix durability, not
change who can see these images.

Because objects are public-read, the database stores the **stable, public
object URL** returned by `save()` (never a signed/expiring URL) —
`profile_picture_url` / `logo_url` keep exactly the shape they always had:
a value the frontend reads and displays as-is. No response-schema or
resolveMediaUrl() changes were needed for the local backend; a bare
one-line fix was needed for cloud URLs specifically (see below).

## Frontend

`resolveMediaUrl()` (`frontend/src/api/client.js`) now returns an already-
absolute `http(s)://` value unchanged **before** trying to prefix it with
the backend origin, so a cloud object URL is never mangled into
`https://api.example.com/https://bucket.s3.../...`. Relative `/media/...`
paths (the local backend, and any legacy un-migrated DB row) resolve
exactly as they did before.

## Migrating existing local files

```bash
python -m app.scripts.migrate_media_to_object_storage --dry-run
python -m app.scripts.migrate_media_to_object_storage
```

- Never runs automatically (not on startup, not on a schedule).
- Idempotent — a row that already holds an absolute cloud URL is skipped.
- For each `User.profile_picture_url` / `Organization.logo_url` /
  `Project.logo_url` starting with `/media/...`: confirms the local file
  exists, uploads it to whichever backend is currently configured, and
  only then updates the DB row. Local files are left in place by default;
  pass `--delete-local-after-migrate` to remove each one once its own
  upload + DB update have both succeeded.
- A missing local file or a failed upload for one row is reported and
  never blocks the rest of the run.

## Deployment checklist

1. Create the bucket/container in your chosen provider.
2. Configure the bucket's access policy for public-read objects (or design
   a signed-URL follow-up if you want private media instead — see above).
3. Configure the backend's credentials/identity — prefer an instance/task
   role over long-lived access keys.
4. Set `ENVIRONMENT=production`, `MEDIA_STORAGE_BACKEND=s3`, `MEDIA_BUCKET`,
   `AWS_REGION` (and `AWS_S3_ENDPOINT_URL`/`MEDIA_PUBLIC_BASE_URL` if
   applicable).
5. If this deployment already has existing local media files, run the
   migration script (dry-run first).
6. Deploy.
7. Test an avatar/logo upload end-to-end.
8. Restart/redeploy the backend and confirm the image still loads.
9. If running multiple instances, confirm an avatar uploaded via one
   instance renders immediately from another (this is the actual
   correctness property this migration exists to guarantee).

## What this migration deliberately did not touch

Generated report PDFs (`PDF_OUTPUT_DIR`, under the same `media/` root)
still use the local filesystem directly. They're a different kind of
artifact — generated on demand from data already in Postgres, not
user-uploaded originals — and migrating them wasn't in scope here. If they
need the same durability treatment later, `app/services/storage/` is
already there to extend to that flow too.
