from datetime import datetime

from pydantic import BaseModel


class ActivityActorSummary(BaseModel):
    """Only what the Activity UI needs to render "who" — never email or
    any auth/security field.

    `name` always prefers the immutable historical `actor_label` snapshot
    (Task #8 follow-up) — captured server-side at the moment of the
    action and never rewritten, even if the live User is later renamed —
    falling back to the live user's current name only for pre-migration
    rows that predate the snapshot, and finally to "Deleted User" only
    when neither is available.

    `id` and `profile_picture_url` come from the *live* User relation, so
    they are None whenever the acting user's account has since been
    deleted (actor_user_id SET NULL) — `is_deleted` makes that explicit
    for the frontend rather than making it infer deletion from `id` being
    absent."""

    id: int | None
    name: str
    profile_picture_url: str | None = None
    is_deleted: bool = False


class ActivityLogRead(BaseModel):
    id: int
    action: str
    actor: ActivityActorSummary
    entity_type: str | None = None
    entity_id: int | None = None
    entity_label: str | None = None
    metadata: dict | None = None
    created_at: datetime

    model_config = {"populate_by_name": True}


class ActivityLogPage(BaseModel):
    items: list[ActivityLogRead]
    page: int
    page_size: int
    total: int
