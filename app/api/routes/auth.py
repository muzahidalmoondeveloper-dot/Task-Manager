import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access_token_bearer import AccessTokenBearer
from app.core.auth_errors import AppException, AuthError, ErrorDef, TokenError
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.rate_limiter import RateLimiter, get_rate_limiter
from app.core.redis_client import get_redis
from app.core.org_roles import TEAM_MEMBER
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    hash_token,
    validate_password_strength,
    verify_password,
)
from app.core.token_cache import TokenCache, get_token_cache
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.repositories.onboarding_repository import ClientOnboardingRepository, OnboardingTemplateRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.schemas.onboarding import ClientOnboardingCreate
from app.schemas.auth import (
    AcceptInvitationRequest,
    AuthenticatedUserResponse,
    ForgotPasswordRequest,
    LoginPasswordResponse,
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    RegisterAndAcceptInvitationRequest,
    ResendOTPRequest,
    ResetPasswordRequest,
    TokenResponse,
    VerifyLoginOTPRequest,
    VerifyRegisterOTPRequest,
)
from app.schemas.organization import OrgSummary
from app.schemas.user import UserCreate, UserRead
from app.services.auth_security_service import AuthSecurityService, get_client_ip
from fastapi import status as http_status

router = APIRouter(prefix="/auth", tags=["Auth"])


_INVITATION_EXPIRED = ErrorDef(
    code="INVITATION_EXPIRED",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Invitation has expired.",
)
_INVITATION_ACCEPTED = ErrorDef(
    code="INVITATION_ALREADY_ACCEPTED",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Invitation has already been accepted.",
)
_INVITATION_NOT_FOUND = ErrorDef(
    code="INVITATION_NOT_FOUND",
    status=http_status.HTTP_404_NOT_FOUND,
    message="Invitation not found.",
)
_INVITATION_REVOKED = ErrorDef(
    code="INVITATION_REVOKED",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="This invitation has been revoked.",
)
_ORG_NOT_FOUND = ErrorDef(
    code="ORG_NOT_FOUND",
    status=http_status.HTTP_404_NOT_FOUND,
    message="Organization not found.",
)
_NOT_ORG_MEMBER = ErrorDef(
    code="NOT_ORG_MEMBER",
    status=http_status.HTTP_403_FORBIDDEN,
    message="You are not a member of this organization.",
)


async def _issue_token_pair(
    user: User,
    db: AsyncSession,
    token_cache: TokenCache,
    org_id: uuid.UUID | None = None,
    org_role: str | None = None,
    org_status: str | None = None,
    org_is_admin: bool = False,
    org_is_team_manager: bool = False,
    org_is_project_manager: bool = False,
) -> TokenResponse:
    """Issue access + refresh tokens, persist the refresh token."""
    access_token, _jti, exp = create_access_token(
        subject=str(user.id),
        extra_claims={"email": user.email},
        org_id=org_id,
        org_role=org_role,
        org_is_admin=org_is_admin,
        org_is_team_manager=org_is_team_manager,
        org_is_project_manager=org_is_project_manager,
    )
    refresh_str, refresh_hash, refresh_exp = create_refresh_token(
        subject=str(user.id), org_id=org_id, org_role=org_role, org_is_admin=org_is_admin,
        org_is_team_manager=org_is_team_manager, org_is_project_manager=org_is_project_manager,
    )

    token_repo = RefreshTokenRepository(db)
    await token_repo.save(
        token_hash=refresh_hash,
        user_id=user.id,
        expires_at=datetime.fromtimestamp(refresh_exp, tz=timezone.utc),
    )
    await db.commit()
    await token_cache.clear_user_access_token_blacklist(str(user.id))

    user_read = UserRead.model_validate(user)
    if org_role is not None:
        # The role that matters to the frontend is the user's role within the
        # *current* organization, not their global default — override it here
        # so `user.role` always reflects org_role from the active tenant context.
        user_read = user_read.model_copy(update={
            "role": org_role,
            "is_org_admin": org_is_admin,
            "is_team_manager": org_is_team_manager,
            "is_project_manager": org_is_project_manager,
        })

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_str,
        expires_at=exp,
        user=user_read,
        org_status=org_status,
    )


async def _resolve_org_for_user(
    user: User,
    db: AsyncSession,
) -> tuple[uuid.UUID | None, str | None, str | None, list[OrgSummary], bool, bool, bool]:
    """Return (org_id, org_role, org_status, all_orgs, org_is_admin, org_is_team_manager, org_is_project_manager).

    Selection priority:
      1. user.last_active_organization_id  (if still an active member)
      2. earliest joined active org (fallback)
      3. None if the user belongs to no active org
    """
    result = await db.execute(
        select(Organization, OrganizationMembership)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.is_active.is_(True),
            Organization.is_active.is_(True),
        )
        .order_by(OrganizationMembership.joined_at.asc())
    )
    rows = result.all()

    if not rows:
        return None, None, None, [], False, False, False

    # Determine the org to activate
    selected_id: uuid.UUID | None = None
    selected_role: str | None = None
    selected_status: str | None = None
    selected_is_admin: bool = False
    selected_is_team_manager: bool = False
    selected_is_project_manager: bool = False

    if user.last_active_organization_id:
        for org, membership in rows:
            if org.id == user.last_active_organization_id:
                selected_id = org.id
                selected_role = membership.role
                selected_status = org.status
                selected_is_admin = membership.is_org_admin
                selected_is_team_manager = membership.is_team_manager
                selected_is_project_manager = membership.is_project_manager
                break

    # Fall back to earliest joined org
    if selected_id is None:
        org, membership = rows[0]
        selected_id = org.id
        selected_role = membership.role
        selected_status = org.status
        selected_is_admin = membership.is_org_admin
        selected_is_team_manager = membership.is_team_manager
        selected_is_project_manager = membership.is_project_manager

    orgs = [
        OrgSummary(
            id=org.id,
            name=org.name,
            slug=org.slug,
            plan=org.plan,
            logo_url=org.logo_url,
            role=membership.role,
            status=org.status,
            is_current=(org.id == selected_id),
        )
        for org, membership in rows
    ]

    return selected_id, selected_role, selected_status, orgs, selected_is_admin, selected_is_team_manager, selected_is_project_manager


# ── Registration ──────────────────────────────────────────────────────────────

@router.post("/register")
async def register(
    payload: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    redis: Redis = Depends(get_redis),
):
    if await rate_limiter.is_rate_limited("register", request):
        raise AuthError.rate_limited()

    pwd_check = validate_password_strength(payload.password)
    if not pwd_check["valid"]:
        raise AuthError.invalid_password(pwd_check["errors"])

    lock_key = f"register_lock:{payload.email.lower().strip()}"
    lock = redis.lock(lock_key, timeout=10)

    try:
        acquired = await lock.acquire(blocking=True, blocking_timeout=5)
        if not acquired:
            raise AuthError.rate_limited("Registration in progress. Please try again.")

        user_repo = UserRepository(db)
        existing_user = await user_repo.get_by_email(payload.email)

        if existing_user:
            if existing_user.email_verified_at is None and not existing_user.is_active:
                security_service = AuthSecurityService(db)
                await security_service.create_and_send_otp(
                    user=existing_user, email=existing_user.email, purpose="register"
                )
                return {
                    "message": "You have already registered. A new OTP has been sent to your email.",
                    "email": existing_user.email,
                }
            raise AuthError.email_exists()

        payload.role = TEAM_MEMBER
        user = await user_repo.create(payload)
        user.email_verified_at = None
        user.is_active = False
        await db.commit()
        await db.refresh(user)

        security_service = AuthSecurityService(db)
        await security_service.create_and_send_otp(user=user, email=user.email, purpose="register")

        return {
            "message": "Registration successful. Please verify the OTP sent to your email.",
            "email": user.email,
        }
    except Exception:
        raise
    finally:
        try:
            await lock.release()
        except Exception:
            pass


@router.post("/register/verify-otp", response_model=TokenResponse)
async def verify_register_otp(
    payload: VerifyRegisterOTPRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    if await rate_limiter.is_rate_limited("verify_otp", request):
        raise AuthError.rate_limited()

    ip_address = get_client_ip(request)
    security_service = AuthSecurityService(db)

    otp = await security_service.verify_otp(
        email=str(payload.email), otp_code=payload.otp_code, purpose="register", ip_address=ip_address
    )

    if otp.user_id is None:
        raise AuthError.invalid_otp()

    user = await db.get(User, otp.user_id)
    if user is None:
        raise AuthError.user_not_found()

    user.email_verified_at = datetime.now(timezone.utc)
    user.is_active = True
    await db.flush()
    await db.commit()
    await db.refresh(user)

    # No organization yet — issue a token without org context so the frontend's
    # ProtectedRoute redirects the user to /setup/organization to create their
    # own workspace (instead of auto-creating one on their behalf).
    return await _issue_token_pair(user, db, token_cache, org_id=None, org_role=None)


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginPasswordResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    if await rate_limiter.is_rate_limited("login", request):
        raise AuthError.rate_limited()

    user_repo = UserRepository(db)
    user = await user_repo.get_by_email(payload.email)

    if user is None or not verify_password(payload.password, user.hashed_password):
        raise AuthError.invalid_credentials()

    if user.email_verified_at is None:
        security_service = AuthSecurityService(db)
        await security_service.create_and_send_otp(user=user, email=user.email, purpose="register")
        return LoginPasswordResponse(
            otp_required=False,
            email_verification_required=True,
            message="Your email is not verified. OTP sent to your email.",
            email=user.email,
        )

    if not user.is_active:
        raise AuthError.account_inactive()

    security_service = AuthSecurityService(db)

    if security_service.login_otp_required(user):
        await security_service.create_and_send_otp(user=user, email=user.email, purpose="login")
        return LoginPasswordResponse(
            otp_required=True,
            email_verification_required=False,
            message="OTP sent to your email.",
            email=user.email,
        )

    # Resolve org context
    org_id, org_role, org_status, orgs, org_is_admin, org_is_team_manager, org_is_project_manager = await _resolve_org_for_user(user, db)
    token_pair = await _issue_token_pair(
        user, db, token_cache, org_id=org_id, org_role=org_role, org_status=org_status, org_is_admin=org_is_admin,
        org_is_team_manager=org_is_team_manager, org_is_project_manager=org_is_project_manager,
    )

    return LoginPasswordResponse(
        otp_required=False,
        email_verification_required=False,
        message="Login successful.",
        email=user.email,
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token,
        expires_at=token_pair.expires_at,
        token_type="bearer",
        user=token_pair.user,
        org_status=org_status,
        requires_org_selection=org_id is None and len(orgs) > 1,
        organizations=orgs if org_id is None else [],
    )


@router.post("/login/verify-otp", response_model=LoginPasswordResponse)
async def verify_login_otp(
    payload: VerifyLoginOTPRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    if await rate_limiter.is_rate_limited("verify_otp", request):
        raise AuthError.rate_limited()

    ip_address = get_client_ip(request)
    security_service = AuthSecurityService(db)

    otp = await security_service.verify_otp(
        email=str(payload.email), otp_code=payload.otp_code, purpose="login", ip_address=ip_address
    )

    if otp.user_id is None:
        raise AuthError.invalid_otp()

    user = await db.get(User, otp.user_id)
    if user is None:
        raise AuthError.user_not_found()
    if not user.is_active:
        raise AuthError.account_inactive()

    user.last_login_otp_verified_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)

    org_id, org_role, org_status, orgs, org_is_admin, org_is_team_manager, org_is_project_manager = await _resolve_org_for_user(user, db)
    token_pair = await _issue_token_pair(
        user, db, token_cache, org_id=org_id, org_role=org_role, org_status=org_status, org_is_admin=org_is_admin,
        org_is_team_manager=org_is_team_manager, org_is_project_manager=org_is_project_manager,
    )

    return LoginPasswordResponse(
        otp_required=False,
        email_verification_required=False,
        message="Login successful.",
        email=user.email,
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token,
        expires_at=token_pair.expires_at,
        token_type="bearer",
        user=token_pair.user,
        org_status=org_status,
        requires_org_selection=org_id is None and len(orgs) > 1,
        organizations=orgs if org_id is None else [],
    )


# ── Organization selection ────────────────────────────────────────────────────

@router.get("/my-organizations")
async def my_organizations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    token_payload: dict = Depends(AccessTokenBearer()),
):
    """Return all organizations the current user belongs to, with is_current flag per JWT."""
    current_org_id: uuid.UUID | None = None
    raw_org_id = token_payload.get("org_id")
    if raw_org_id:
        try:
            current_org_id = uuid.UUID(str(raw_org_id))
        except ValueError:
            pass

    result = await db.execute(
        select(Organization, OrganizationMembership)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(
            OrganizationMembership.user_id == current_user.id,
            OrganizationMembership.is_active.is_(True),
            Organization.is_active.is_(True),
        )
        .order_by(OrganizationMembership.joined_at.asc())
    )
    rows = result.all()

    orgs = [
        OrgSummary(
            id=org.id,
            name=org.name,
            slug=org.slug,
            plan=org.plan,
            logo_url=org.logo_url,
            role=membership.role,
            status=org.status,
            is_current=(org.id == current_org_id),
        )
        for org, membership in rows
    ]

    return {"organizations": orgs, "current": str(current_org_id) if current_org_id else None}


@router.post("/select-organization/{org_id}", response_model=TokenResponse)
async def select_organization(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
):
    """Issue a new org-scoped token pair for the given organization."""
    result = await db.execute(
        select(Organization).where(Organization.id == org_id, Organization.is_active.is_(True))
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise AppException(_ORG_NOT_FOUND)

    result = await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == current_user.id,
            OrganizationMembership.is_active.is_(True),
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise AppException(_NOT_ORG_MEMBER)

    # Persist the chosen org so it is auto-restored on next login
    current_user.last_active_organization_id = org_id
    await db.flush()
    # `users.updated_at` is server-evaluated (onupdate=func.now()) — the row was
    # just updated, so SQLAlchemy marks that column unloaded on this instance.
    # Refresh now (inside an awaited call) so the later synchronous Pydantic
    # read in _issue_token_pair doesn't trigger an async lazy-load.
    await db.refresh(current_user)

    return await _issue_token_pair(
        current_user, db, token_cache, org_id=org_id, org_role=membership.role, org_status=org.status,
        org_is_admin=membership.is_org_admin,
        org_is_team_manager=membership.is_team_manager, org_is_project_manager=membership.is_project_manager,
    )


async def _create_onboarding_from_invitation(db: AsyncSession, invitation, client_user_id: int) -> int | None:
    """Client-invitation acceptance side effect: create the ClientOnboarding
    record from the invitation's template so the client lands straight on
    their checklist. `invite_client` already validated the template exists
    and has steps before the invitation was ever sent, so this should always
    resolve to a real template; ClientOnboardingRepository.create() still
    enforces it (raises rather than creating a blank onboarding) as a last
    line of defense against a template being deleted in between. No-ops if
    an onboarding already exists for this client+project (defends against a
    double-accept race)."""
    if invitation.project_id is None:
        return None

    onboarding_repo = ClientOnboardingRepository(db, invitation.organization_id)
    existing = await onboarding_repo.list_all(client_user_id=client_user_id)
    for record in existing:
        if record.project_id == invitation.project_id:
            return record.id

    template = None
    if invitation.onboarding_template_id is not None:
        template_repo = OnboardingTemplateRepository(db, invitation.organization_id)
        template = await template_repo.get_by_id(invitation.onboarding_template_id)

    onboarding = await onboarding_repo.create(
        ClientOnboardingCreate(
            client_user_id=client_user_id,
            project_id=invitation.project_id,
            project_manager_id=invitation.project_manager_id,
            template_id=invitation.onboarding_template_id if template else None,
            due_date=invitation.due_date,
        ),
        created_by_id=invitation.invited_by_id,
        template=template,
    )
    return onboarding.id


# ── Accept invitation ─────────────────────────────────────────────────────────

@router.post("/accept-invitation", response_model=TokenResponse)
async def accept_invitation(
    payload: AcceptInvitationRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
):
    org_repo = OrganizationRepository(db)
    invitation = await org_repo.get_invitation_by_token(payload.token)

    if invitation is None:
        raise AppException(_INVITATION_NOT_FOUND)

    if invitation.accepted_at is not None:
        raise AppException(_INVITATION_ACCEPTED)

    if invitation.status == "revoked":
        raise AppException(_INVITATION_REVOKED)

    now = datetime.now(timezone.utc)
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise AppException(_INVITATION_EXPIRED)

    # Check if already a member
    existing = await org_repo.get_membership(invitation.organization_id, current_user.id)
    if existing is None or not existing.is_active:
        membership = await org_repo.add_member(
            invitation.organization_id, current_user.id, invitation.role
        )
    else:
        membership = existing

    if invitation.project_id is not None:
        project_repo = ProjectRepository(db, invitation.organization_id)
        await project_repo.add_member(invitation.project_id, current_user.id)
        invitation.onboarding_id = await _create_onboarding_from_invitation(db, invitation, current_user.id)

    await org_repo.accept_invitation(invitation)
    await db.commit()

    return await _issue_token_pair(
        current_user, db, token_cache,
        org_id=invitation.organization_id,
        org_role=membership.role,
        org_is_admin=membership.is_org_admin,
        org_is_team_manager=membership.is_team_manager,
        org_is_project_manager=membership.is_project_manager,
    )


@router.post("/register-and-accept-invitation", response_model=TokenResponse)
async def register_and_accept_invitation(
    payload: RegisterAndAcceptInvitationRequest,
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
):
    """One-step invitation acceptance for people with no existing account
    (primarily Client invitations) — creates the User, org membership, and
    (if the invitation is project-scoped) ProjectMembership, all at once.
    The invitation token itself is the proof of email ownership, so no
    separate OTP step is required, mirroring how token-based flows already
    behave elsewhere in this app."""
    pwd_check = validate_password_strength(payload.password)
    if not pwd_check["valid"]:
        raise AuthError.invalid_password(pwd_check["errors"])

    org_repo = OrganizationRepository(db)
    invitation = await org_repo.get_invitation_by_token(payload.token)

    if invitation is None:
        raise AppException(_INVITATION_NOT_FOUND)
    if invitation.accepted_at is not None:
        raise AppException(_INVITATION_ACCEPTED)

    if invitation.status == "revoked":
        raise AppException(_INVITATION_REVOKED)

    now = datetime.now(timezone.utc)
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise AppException(_INVITATION_EXPIRED)

    user_repo = UserRepository(db)
    existing_user = await user_repo.get_by_email(invitation.email)
    if existing_user is not None:
        raise AuthError.email_exists()

    user = await user_repo.create(UserCreate(
        full_name=payload.full_name,
        email=invitation.email,
        password=payload.password,
    ))
    user.email_verified_at = now

    membership = await org_repo.add_member(invitation.organization_id, user.id, invitation.role)

    if invitation.project_id is not None:
        project_repo = ProjectRepository(db, invitation.organization_id)
        await project_repo.add_member(invitation.project_id, user.id)
        invitation.onboarding_id = await _create_onboarding_from_invitation(db, invitation, user.id)

    await org_repo.accept_invitation(invitation)
    await db.commit()
    await db.refresh(user)

    return await _issue_token_pair(
        user, db, token_cache,
        org_id=invitation.organization_id,
        org_role=membership.role,
        org_is_admin=membership.is_org_admin,
        org_is_team_manager=membership.is_team_manager,
        org_is_project_manager=membership.is_project_manager,
    )


# ── Invitation preview (public) ───────────────────────────────────────────────

@router.get("/invitation-preview")
async def invitation_preview(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint — returns invitation details without requiring auth.
    Used by the accept-invitation page to show org/role context before login."""
    org_repo = OrganizationRepository(db)
    invitation = await org_repo.get_invitation_by_token(token)

    if invitation is None:
        raise AppException(_INVITATION_NOT_FOUND)

    if invitation.status == "revoked":
        raise AppException(_INVITATION_REVOKED)

    now = datetime.now(timezone.utc)
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise AppException(_INVITATION_EXPIRED)

    if invitation.accepted_at is not None:
        raise AppException(_INVITATION_ACCEPTED)

    inviter_name = None
    if invitation.invited_by:
        inviter_name = invitation.invited_by.full_name

    user_repo = UserRepository(db)
    existing_user = await user_repo.get_by_email(invitation.email)

    project_name = None
    if invitation.project_id is not None:
        project_repo = ProjectRepository(db, invitation.organization_id)
        project = await project_repo.get_by_id(invitation.project_id)
        project_name = project.name if project else None

    # Record that the invitee opened the link — client invitations surface
    # this as an "opened" status on the invitations management list.
    org_repo = OrganizationRepository(db)
    if invitation.status == "sent":
        await org_repo.mark_invitation_opened(invitation)
        await db.commit()

    return {
        "email": invitation.email,
        "role": invitation.role,
        "organization_name": invitation.organization.name if invitation.organization else None,
        "organization_id": str(invitation.organization_id),
        "project_id": invitation.project_id,
        "project_name": project_name,
        "client_name": invitation.client_name,
        "company_name": invitation.company_name,
        "project_manager_name": invitation.project_manager.full_name if invitation.project_manager else None,
        "onboarding_template_name": invitation.onboarding_template.name if invitation.onboarding_template else None,
        "expires_at": invitation.expires_at.isoformat(),
        "invited_by_name": inviter_name,
        # Lets the accept-invitation page show the single right action —
        # "Log in" if this email already has an account, "Create account" if not.
        "account_exists": existing_user is not None,
    }


# ── Token management ──────────────────────────────────────────────────────────

@router.post("/token-refresh", response_model=TokenResponse)
async def refresh_access_token(
    data: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
):
    payload = decode_refresh_token(data.refresh_token)
    if not payload:
        raise TokenError.invalid()

    user_id = payload.get("sub")
    if not user_id:
        raise TokenError.invalid("Invalid user ID in token.")

    token_hash = hash_token(data.refresh_token)
    token_repo = RefreshTokenRepository(db)
    stored = await token_repo.get_by_hash(token_hash)

    if not stored:
        raise TokenError.invalid()

    if stored.is_revoked:
        await token_repo.revoke_all_for_user(int(user_id))
        await token_cache.revoke_all_user_tokens(user_id)
        await db.commit()
        raise TokenError.revoked()

    expires_at = stored.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise TokenError.expired()

    user = await db.get(User, int(user_id))
    if user is None:
        raise AuthError.user_not_found()
    if not user.is_active:
        raise AuthError.account_inactive()

    await token_repo.revoke(token_hash)

    # Preserve the org context from the old token if available
    # (The client must re-call select-organization if the org context is lost)
    org_id_raw = payload.get("org_id")
    org_role = payload.get("org_role")
    org_is_admin = bool(payload.get("org_is_admin", False))
    org_is_team_manager = bool(payload.get("org_is_team_manager", False))
    org_is_project_manager = bool(payload.get("org_is_project_manager", False))
    try:
        org_id = uuid.UUID(str(org_id_raw)) if org_id_raw else None
    except (ValueError, AttributeError):
        org_id = None

    access_token, _jti, exp = create_access_token(
        subject=str(user.id),
        extra_claims={"email": user.email},
        org_id=org_id,
        org_role=org_role,
        org_is_admin=org_is_admin,
        org_is_team_manager=org_is_team_manager,
        org_is_project_manager=org_is_project_manager,
    )
    new_refresh_str, new_hash, new_exp = create_refresh_token(
        subject=str(user.id), org_id=org_id, org_role=org_role, org_is_admin=org_is_admin,
        org_is_team_manager=org_is_team_manager, org_is_project_manager=org_is_project_manager,
    )
    await token_repo.save(
        token_hash=new_hash,
        user_id=user.id,
        expires_at=datetime.fromtimestamp(new_exp, tz=timezone.utc),
    )
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_str,
        expires_at=exp,
        user=UserRead.model_validate(user),
    )


@router.post("/logout")
async def logout(
    logout_request: LogoutRequest,
    token_payload: dict = Depends(AccessTokenBearer()),
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
):
    jti = token_payload.get("jti")
    exp = token_payload.get("exp")
    user_id = token_payload.get("sub")

    if not jti or not exp or not user_id:
        raise TokenError.invalid("Invalid token payload.")

    token_repo = RefreshTokenRepository(db)

    if logout_request.logout_all_devices:
        await token_repo.revoke_all_for_user(int(user_id))
        await token_cache.revoke_all_user_tokens(user_id)
    else:
        refresh_payload = decode_refresh_token(logout_request.refresh_token)
        if not refresh_payload:
            raise TokenError.invalid("Refresh token is invalid.")
        if refresh_payload.get("sub") != user_id:
            raise TokenError.invalid("Token mismatch.")

        refresh_hash = hash_token(logout_request.refresh_token)
        stored = await token_repo.get_by_hash(refresh_hash)
        if not stored:
            raise TokenError.invalid()
        if stored.is_revoked:
            raise TokenError.invalid("Refresh token already revoked.")
        await token_repo.revoke(refresh_hash)

    await db.commit()
    ttl = max(0, int(exp) - int(time.time()))
    await token_cache.blacklist_access_token(jti, ttl)
    return {"message": "Logged out successfully."}


# ── Profile & OTP helpers ────────────────────────────────────────────────────

@router.get("/me", response_model=AuthenticatedUserResponse)
async def get_me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    token_payload: dict = Depends(AccessTokenBearer()),
):
    user_read = UserRead.model_validate(current_user)
    org_role = token_payload.get("org_role")
    if org_role is not None:
        # Reflect the user's role in their *current* organization rather than
        # their global default — keeps the profile in sync with the active tenant.
        org_is_admin = bool(token_payload.get("org_is_admin", False))
        org_is_team_manager = bool(token_payload.get("org_is_team_manager", False))
        org_is_project_manager = bool(token_payload.get("org_is_project_manager", False))
        user_read = user_read.model_copy(update={
            "role": org_role,
            "is_org_admin": org_is_admin,
            "is_team_manager": org_is_team_manager,
            "is_project_manager": org_is_project_manager,
        })

    org_status: str | None = None
    raw_org_id = token_payload.get("org_id")
    if raw_org_id:
        try:
            org_status = await db.scalar(
                select(Organization.status).where(Organization.id == uuid.UUID(str(raw_org_id)))
            )
        except ValueError:
            pass

    return AuthenticatedUserResponse(user=user_read, org_status=org_status)


@router.post("/resend-otp")
async def resend_otp(
    payload: ResendOTPRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    if await rate_limiter.is_rate_limited("resend_otp", request):
        raise AuthError.rate_limited()

    if payload.purpose not in {"register", "login", "reset_password"}:
        raise AuthError.otp_purpose_invalid()

    user_repo = UserRepository(db)
    user = await user_repo.get_by_email(str(payload.email))
    if user is None:
        raise AuthError.user_not_found()

    if payload.purpose == "register" and user.email_verified_at is not None:
        raise AuthError.email_already_verified()

    if payload.purpose == "login":
        if not user.is_active:
            raise AuthError.account_inactive()
        if user.email_verified_at is None:
            raise AuthError.email_not_verified()

    security_service = AuthSecurityService(db)
    await security_service.create_and_send_otp(user=user, email=user.email, purpose=payload.purpose)
    return {"message": "OTP resent successfully.", "email": user.email, "purpose": payload.purpose}


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    if await rate_limiter.is_rate_limited("forgot_password", request):
        raise AuthError.rate_limited()

    user_repo = UserRepository(db)
    user = await user_repo.get_by_email(str(payload.email))
    if user is not None and user.is_active:
        security_service = AuthSecurityService(db)
        await security_service.create_and_send_otp(user=user, email=user.email, purpose="reset_password")

    return {"message": "If that email is registered, an OTP has been sent.", "email": str(payload.email)}


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    pwd_check = validate_password_strength(payload.new_password)
    if not pwd_check["valid"]:
        raise AuthError.invalid_password(pwd_check["errors"])

    ip_address = get_client_ip(request)
    security_service = AuthSecurityService(db)

    otp = await security_service.verify_otp(
        email=str(payload.email), otp_code=payload.otp_code, purpose="reset_password", ip_address=ip_address
    )

    if otp.user_id is None:
        raise AuthError.invalid_otp()

    user = await db.get(User, otp.user_id)
    if user is None or not user.is_active:
        raise AuthError.user_not_found()

    user.hashed_password = hash_password(payload.new_password)
    await db.commit()
    return {"message": "Password reset successful. You can now log in."}
