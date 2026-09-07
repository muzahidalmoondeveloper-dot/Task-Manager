import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, UploadFile
from fastapi import File as FastAPIFile
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.activity_actions import (
    ENTITY_INVITATION,
    ENTITY_ORGANIZATION,
    ENTITY_USER,
    ORGANIZATION_SETTINGS_UPDATED,
    ORGANIZATION_UPDATED,
    USER_DEACTIVATED,
    USER_INVITED,
    USER_ROLE_CHANGED,
)
from app.core.auth_errors import AppException, AuthError, ErrorDef
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.org_roles import ORG_ROLE_LABELS, OWNER, TEAM_MEMBER
from app.core.plan_limits import get_plan_limits
from app.core.security import create_access_token, create_refresh_token, hash_token
from app.core.tenant import (
    TenantContext,
    check_active_billing,
    enforce_member_limit,
    get_tenant_context,
    require_org_admin,
    require_org_owner,
)
from app.core.token_cache import TokenCache, get_token_cache
from app.models.organization import OrganizationMembership
from app.models.project import Project
from app.models.team import Team
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services import activity_service
from app.schemas.auth import TokenResponse
from app.schemas.organization import (
    AcceptInvitationRequest,
    InvitationRead,
    InviteMemberRequest,
    MembershipRead,
    OrgUsageRead,
    OrganizationCreate,
    OrganizationRead,
    OrganizationSetupComplete,
    OrganizationUpdate,
    PlanLimitsRead,
    ScoreboardWeightsRead,
    ScoreboardWeightsUpdate,
    SlugCheckResponse,
    SubscriptionRead,
    UpdateMemberRoleRequest,
)
from app.schemas.user import UserRead
from app.services import logo_upload_service, stripe_service
from app.services.email_service import EmailService

_LOGO_DIR_NAME = "organization-logos"

# How many organizations a single user may own at once — a simple, generous
# hard cap (plan tiers in this app are per-organization, not per-user, so a
# plan-based limit per the original spec doesn't map cleanly onto this data model).
MAX_OWNED_ORGS = 10


def _normalize_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", value.lower().strip())

_SLUG_TAKEN = ErrorDef(
    code="SLUG_TAKEN",
    status=http_status.HTTP_409_CONFLICT,
    message="This organization slug is already taken.",
)
_ORG_LIMIT_REACHED = ErrorDef(
    code="ORG_LIMIT_REACHED",
    status=http_status.HTTP_403_FORBIDDEN,
    message=f"You can own up to {MAX_OWNED_ORGS} organizations. Transfer or delete one to create another.",
)
_ORG_NOT_FOUND = ErrorDef(
    code="ORG_NOT_FOUND",
    status=http_status.HTTP_404_NOT_FOUND,
    message="Organization not found.",
)
_NOT_ORG_OWNER = ErrorDef(
    code="NOT_ORG_OWNER",
    status=http_status.HTTP_403_FORBIDDEN,
    message="Only the owner can complete organization setup.",
)
_SETUP_ALREADY_DONE = ErrorDef(
    code="SETUP_ALREADY_DONE",
    status=http_status.HTTP_409_CONFLICT,
    message="Organization setup is already complete.",
)
_SLUG_TOO_SHORT = ErrorDef(
    code="SLUG_TOO_SHORT",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Slug must be at least 2 characters.",
)
_ALREADY_MEMBER = ErrorDef(
    code="ALREADY_MEMBER",
    status=http_status.HTTP_409_CONFLICT,
    message="User is already a member of this organization.",
)
_CANNOT_REMOVE_OWNER = ErrorDef(
    code="CANNOT_REMOVE_OWNER",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="The organization owner cannot be removed. Transfer ownership first.",
)
_INVITATION_INVALID = ErrorDef(
    code="INVITATION_INVALID",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Invitation is invalid or has expired.",
)
_INVITATION_ALREADY_ACCEPTED = ErrorDef(
    code="INVITATION_ALREADY_ACCEPTED",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="This invitation has already been accepted.",
)
_BILLING_SETUP_FAILED = ErrorDef(
    code="BILLING_SETUP_FAILED",
    status=http_status.HTTP_502_BAD_GATEWAY,
    message="Could not set up billing for this organization. Please try again.",
)

router = APIRouter(prefix="/organizations", tags=["Organizations"])


async def _issue_org_token_pair(
    user: User,
    org_id: uuid.UUID,
    org_role: str,
    db: AsyncSession,
    token_cache: TokenCache,
    org_status: str | None = None,
    organization: dict | None = None,
) -> TokenResponse:
    access_token, _jti, exp = create_access_token(
        subject=str(user.id),
        extra_claims={"email": user.email, "org_id": str(org_id), "org_role": org_role},
    )
    refresh_str, refresh_hash, refresh_exp = create_refresh_token(subject=str(user.id), org_id=org_id, org_role=org_role)
    token_repo = RefreshTokenRepository(db)
    await token_repo.save(
        token_hash=refresh_hash,
        user_id=user.id,
        expires_at=datetime.fromtimestamp(refresh_exp, tz=timezone.utc),
    )
    await db.commit()
    await token_cache.clear_user_access_token_blacklist(str(user.id))

    # Reflect the user's role in *this* organization, not their global default —
    # keeps the returned profile in sync with the tenant context just switched into.
    user_read = UserRead.model_validate(user).model_copy(update={"role": org_role})

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_str,
        expires_at=exp,
        user=user_read,
        org_status=org_status,
        organization=organization,
    )


# ── Create organization ───────────────────────────────────────────────────────

@router.get("/check-slug", response_model=SlugCheckResponse)
async def check_slug_availability(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Real-time slug availability check for the create-organization form."""
    normalized = _normalize_slug(slug)
    if len(normalized) < 2:
        raise AppException(_SLUG_TOO_SHORT)

    repo = OrganizationRepository(db)
    available = not await repo.slug_exists(normalized)
    return SlugCheckResponse(slug=normalized, available=available)


@router.post("", response_model=TokenResponse, status_code=http_status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
):
    """Any authenticated user can create a new organization, independent of any
    other org memberships they already have (e.g. joined via invitation).
    Returns a new org-scoped token pair so the caller is immediately in context."""
    repo = OrganizationRepository(db)

    owned_count = await repo.count_owned_orgs(current_user.id)
    if owned_count >= MAX_OWNED_ORGS:
        raise AppException(_ORG_LIMIT_REACHED)

    slug = payload.resolved_slug()
    if await repo.slug_exists(slug):
        raise AppException(_SLUG_TAKEN)

    org = await repo.create(
        name=payload.name,
        slug=slug,
        owner_id=current_user.id,
        plan=payload.plan,
        status="pending_setup",
    )
    await repo.add_member(org.id, current_user.id, role=OWNER)

    try:
        customer = stripe_service.create_customer(org, current_user)
        stripe_subscription = stripe_service.create_trial_subscription(
            customer_id=customer["id"],
            plan=payload.plan,
            interval=payload.billing_interval,
            org_id=str(org.id),
        )
    except Exception:
        raise AppException(_BILLING_SETUP_FAILED)

    fields = stripe_service.extract_subscription_fields(stripe_subscription)
    await repo.get_or_create_subscription(
        org.id,
        plan=payload.plan,
        billing_interval=payload.billing_interval,
        status=fields["status"] or "trialing",
        trial_ends_at=fields["trial_ends_at"],
        stripe_customer_id=customer["id"],
        stripe_subscription_id=stripe_subscription["id"],
        current_period_start=fields["current_period_start"],
        current_period_end=fields["current_period_end"],
    )

    # The newly created org becomes the user's active org going forward.
    current_user.last_active_organization_id = org.id
    await db.flush()
    await db.commit()
    # `users.updated_at` is server-evaluated (onupdate=func.now()) — the row was
    # just UPDATEd above, so SQLAlchemy marks that column unloaded on this
    # instance. Refresh now (inside an awaited call) so the later synchronous
    # Pydantic read in _issue_org_token_pair doesn't trigger an async lazy-load.
    await db.refresh(current_user)

    return await _issue_org_token_pair(
        current_user,
        org.id,
        OWNER,
        db,
        token_cache,
        org_status=org.status,
        organization=OrganizationRead.model_validate(org).model_dump(mode="json"),
    )


@router.put("/{org_id}/setup", response_model=OrganizationRead)
async def complete_organization_setup(
    org_id: uuid.UUID,
    payload: OrganizationSetupComplete,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Final step of the creation wizard — saves details and marks the org active.
    Only the owner may complete setup, and only while status is still pending_setup."""
    repo = OrganizationRepository(db)
    org = await repo.get_by_id(org_id)
    if org is None:
        raise AppException(_ORG_NOT_FOUND)
    if org.owner_id != current_user.id:
        raise AppException(_NOT_ORG_OWNER)
    if org.status != "pending_setup":
        raise AppException(_SETUP_ALREADY_DONE)

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    data["status"] = "active"
    org = await repo.update(org, data)
    await db.commit()
    await db.refresh(org)

    return OrganizationRead.model_validate(org)


# ── Read / update / deactivate ────────────────────────────────────────────────

@router.get("/current", response_model=OrganizationRead)
async def get_current_org(tenant: TenantContext = Depends(get_tenant_context)):
    return OrganizationRead.model_validate(tenant.organization)


@router.put("/current", response_model=OrganizationRead)
async def update_current_org(
    payload: OrganizationUpdate,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(db)
    data = payload.model_dump(exclude_unset=True)
    previous_name = tenant.organization.name
    org = await repo.update(tenant.organization, data)
    await db.commit()
    await db.refresh(org)

    # Task #8B — meaningful field names only, never the whole settings
    # object; the org's display name is the one field where the safe
    # scalar before/after value itself is worth recording.
    changed_fields = list(data.keys())
    if changed_fields:
        metadata = {"fields_changed": changed_fields}
        if "name" in data and data["name"] != previous_name:
            metadata["name_from"] = previous_name
            metadata["name_to"] = data["name"]
        await activity_service.record(
            db, organization_id=tenant.organization_id, actor=tenant.user,
            action=ORGANIZATION_UPDATED, entity_type=ENTITY_ORGANIZATION, entity_id=None,
            entity_label=org.name, metadata=metadata,
        )

    return OrganizationRead.model_validate(org)


@router.post("/current/logo", response_model=OrganizationRead)
async def upload_current_org_logo(
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
    file: UploadFile = FastAPIFile(...),
):
    org = tenant.organization
    new_url = await logo_upload_service.save_logo(file, _LOGO_DIR_NAME, org.id)

    # Save-then-commit-then-delete-old — never delete the existing logo
    # before the replacement is confirmed persisted (see
    # app/services/storage/base.py and the avatar upload route for the
    # same ordering). If the commit itself fails, clean up the just-
    # uploaded object instead of leaving it orphaned.
    previous_url = org.logo_url
    try:
        org.logo_url = new_url
        await db.commit()
    except Exception:
        await db.rollback()
        await logo_upload_service.delete_logo_file(new_url, _LOGO_DIR_NAME)
        raise

    await logo_upload_service.delete_logo_file(previous_url, _LOGO_DIR_NAME)
    await db.refresh(org)
    return OrganizationRead.model_validate(org)


@router.delete("/current/logo", response_model=OrganizationRead)
async def delete_current_org_logo(
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    org = tenant.organization
    previous_url = org.logo_url
    org.logo_url = None
    await db.commit()
    await db.refresh(org)

    await logo_upload_service.delete_logo_file(previous_url, _LOGO_DIR_NAME)
    return OrganizationRead.model_validate(org)


@router.get("/current/scoreboard-weights", response_model=ScoreboardWeightsRead)
async def get_scoreboard_weights(tenant: TenantContext = Depends(get_tenant_context)):
    return ScoreboardWeightsRead.model_validate(tenant.organization)


@router.put("/current/scoreboard-weights", response_model=ScoreboardWeightsRead)
async def update_scoreboard_weights(
    payload: ScoreboardWeightsUpdate,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    org = tenant.organization
    org.scoreboard_completion_weight = payload.scoreboard_completion_weight
    org.scoreboard_on_time_weight = payload.scoreboard_on_time_weight
    org.scoreboard_overdue_weight = payload.scoreboard_overdue_weight
    await db.commit()
    await db.refresh(org)

    # Task #8B — a genuine configuration change (affects real scoring
    # behavior across the org), not a cosmetic UI preference, so it
    # qualifies as organization.settings_updated. Safe bounded scalars
    # only — never a dump of the whole settings/org object.
    await activity_service.record(
        db, organization_id=tenant.organization_id, actor=tenant.user,
        action=ORGANIZATION_SETTINGS_UPDATED, entity_type=ENTITY_ORGANIZATION, entity_id=None,
        entity_label=org.name,
        metadata={
            "completion_weight": org.scoreboard_completion_weight,
            "on_time_weight": org.scoreboard_on_time_weight,
            "overdue_weight": org.scoreboard_overdue_weight,
        },
    )

    return ScoreboardWeightsRead.model_validate(org)


@router.delete("/current", status_code=http_status.HTTP_204_NO_CONTENT)
async def deactivate_current_org(
    tenant: TenantContext = Depends(require_org_owner),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(db)
    await repo.deactivate(tenant.organization)
    await db.commit()


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/current/members", response_model=list[MembershipRead])
async def list_members(
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(db)
    rows = await repo.list_members(tenant.organization_id)
    return [
        MembershipRead(
            id=m.id,
            organization_id=m.organization_id,
            user_id=m.user_id,
            role=m.role,
            is_org_admin=m.is_org_admin,
            is_active=m.is_active,
            joined_at=m.joined_at,
            user=UserRead.model_validate(u).model_copy(update={"role": m.role, "is_org_admin": m.is_org_admin}),
        )
        for m, u in rows
    ]


@router.delete("/current/members/{user_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: int,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    if user_id == tenant.organization.owner_id:
        raise AppException(_CANNOT_REMOVE_OWNER)

    repo = OrganizationRepository(db)
    membership = await repo.get_membership(tenant.organization_id, user_id)
    if membership is None or not membership.is_active:
        raise AuthError.user_not_found()

    from app.repositories.user_repository import UserRepository as _UserRepository
    removed_user = await _UserRepository(db).get_by_id(user_id)

    await repo.remove_member(membership)
    await db.commit()

    # Task #8B — remove_member() is a soft membership deactivation
    # (`membership.is_active = False`), not a hard delete — maps to
    # user.deactivated, the same event update_user() emits for an
    # is_active toggle, just reached through this separate route.
    await activity_service.record(
        db, organization_id=tenant.organization_id, actor=tenant.user,
        action=USER_DEACTIVATED, entity_type=ENTITY_USER, entity_id=user_id,
        entity_label=removed_user.full_name if removed_user else None,
    )


@router.put("/current/members/{user_id}/role", response_model=MembershipRead)
async def update_member_role(
    user_id: int,
    payload: UpdateMemberRoleRequest,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    if user_id == tenant.organization.owner_id and payload.role != OWNER:
        raise AppException(_CANNOT_REMOVE_OWNER)

    repo = OrganizationRepository(db)
    membership = await repo.get_membership(tenant.organization_id, user_id)
    if membership is None or not membership.is_active:
        raise AuthError.user_not_found()

    role_before = membership.role
    updated = await repo.update_member_role(membership, payload.role)
    await db.commit()
    await db.refresh(updated)

    from app.repositories.user_repository import UserRepository
    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(user_id)

    # Task #8B — this is a second, separate role-change route from
    # users.py's PATCH /users/{id} (which already logs USER_ROLE_CHANGED)
    # — was previously a real audit gap: role changes made through this
    # endpoint were completely unlogged. Same action + metadata shape as
    # the other route for a consistent history regardless of which UI
    # surface made the change.
    if role_before != payload.role:
        await activity_service.record(
            db, organization_id=tenant.organization_id, actor=tenant.user,
            action=USER_ROLE_CHANGED, entity_type=ENTITY_USER, entity_id=user_id,
            entity_label=user.full_name if user else None,
            metadata={"from_role": role_before, "to_role": payload.role},
        )

    return MembershipRead(
        id=updated.id,
        organization_id=updated.organization_id,
        user_id=updated.user_id,
        role=updated.role,
        is_org_admin=updated.is_org_admin,
        is_active=updated.is_active,
        joined_at=updated.joined_at,
        user=UserRead.model_validate(user).model_copy(update={"role": updated.role, "is_org_admin": updated.is_org_admin}),
    )


# ── Invitations ───────────────────────────────────────────────────────────────

@router.post(
    "/current/members/invite",
    response_model=InvitationRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def invite_member(
    payload: InviteMemberRequest,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    check_active_billing(tenant)
    await enforce_member_limit(tenant, db)

    repo = OrganizationRepository(db)
    from app.repositories.user_repository import UserRepository

    # Check if user is already a member
    user_repo = UserRepository(db)
    existing_user = await user_repo.get_by_email(str(payload.email))
    if existing_user:
        existing_membership = await repo.get_membership(tenant.organization_id, existing_user.id)
        if existing_membership and existing_membership.is_active:
            raise AppException(_ALREADY_MEMBER)

    invitation = await repo.create_invitation(
        org_id=tenant.organization_id,
        email=str(payload.email),
        role=payload.role,
        invited_by_id=tenant.user.id,
    )
    await db.commit()
    await db.refresh(invitation)

    # Send invitation email
    email_service = EmailService()
    email_service.send_invitation_email(
        to_email=str(payload.email),
        org_name=tenant.organization.name,
        inviter_name=tenant.user.full_name,
        token=invitation.token,
        role=invitation.role,
    )

    # Task #8B — the raw invited email is deliberately never persisted
    # into activity_metadata/entity_label (avoid permanently storing an
    # external person's email in an append-only log); a neutral role-based
    # label is used instead. entity_type is "invitation", not "user" — no
    # User row exists yet, distinguishing this from user.created.
    role_label = ORG_ROLE_LABELS.get(invitation.role, invitation.role)
    await activity_service.record(
        db, organization_id=tenant.organization_id, actor=tenant.user,
        action=USER_INVITED, entity_type=ENTITY_INVITATION, entity_id=None,
        entity_label=f"{role_label} invitation", metadata={"role": invitation.role},
    )

    return InvitationRead.model_validate(invitation)


@router.get("/current/invitations", response_model=list[InvitationRead])
async def list_pending_invitations(
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(db)
    invitations = await repo.list_pending_invitations(tenant.organization_id)
    return [InvitationRead.model_validate(i) for i in invitations]


@router.delete(
    "/current/invitations/{invitation_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a pending invitation so the link is immediately invalidated."""
    repo = OrganizationRepository(db)
    invitation = await repo.get_invitation_by_id(tenant.organization_id, invitation_id)
    if invitation is None or invitation.accepted_at is not None:
        raise AppException(_INVITATION_INVALID)
    await db.delete(invitation)
    await db.commit()
    return None


@router.post(
    "/current/invitations/{invitation_id}/resend",
    response_model=InvitationRead,
)
async def resend_invitation(
    invitation_id: uuid.UUID,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Regenerate the invitation token, extend expiry by 72 h, and resend the email."""
    repo = OrganizationRepository(db)
    invitation = await repo.get_invitation_by_id(tenant.organization_id, invitation_id)
    if invitation is None or invitation.accepted_at is not None:
        raise AppException(_INVITATION_INVALID)

    invitation.token = secrets.token_urlsafe(48)
    invitation.expires_at = datetime.now(timezone.utc) + timedelta(hours=72)
    await db.flush()
    await db.commit()
    await db.refresh(invitation)

    email_service = EmailService()
    email_service.send_invitation_email(
        to_email=invitation.email,
        org_name=tenant.organization.name,
        inviter_name=tenant.user.full_name,
        token=invitation.token,
        role=invitation.role,
    )

    return InvitationRead.model_validate(invitation)


# ── Subscription & Usage ──────────────────────────────────────────────────────

@router.get("/current/subscription", response_model=SubscriptionRead)
async def get_subscription(
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(db)
    sub = await repo.get_or_create_subscription(tenant.organization_id, tenant.organization.plan)
    await db.commit()
    return SubscriptionRead.model_validate(sub)


@router.get("/current/usage", response_model=OrgUsageRead)
async def get_usage(
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(db)
    limits = tenant.plan_limits

    member_count = await repo.count_active_members(tenant.organization_id)

    team_result = await db.execute(
        select(func.count(Team.id)).where(Team.organization_id == tenant.organization_id)
    )
    team_count = team_result.scalar_one() or 0

    project_result = await db.execute(
        select(func.count(Project.id)).where(Project.organization_id == tenant.organization_id)
    )
    project_count = project_result.scalar_one() or 0

    return OrgUsageRead(
        plan=tenant.organization.plan,
        limits=PlanLimitsRead(
            max_members=limits.max_members,
            max_teams=limits.max_teams,
            max_projects=limits.max_projects,
            max_tasks_per_month=limits.max_tasks_per_month,
            has_ai_features=limits.has_ai_features,
            has_integrations=limits.has_integrations,
            has_api_access=limits.has_api_access,
            storage_gb=limits.storage_gb,
        ),
        current_members=member_count,
        current_teams=team_count,
        current_projects=project_count,
    )
