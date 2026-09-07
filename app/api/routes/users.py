import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi import File as FastAPIFile
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access_token_bearer import AccessTokenBearer
from app.core.activity_actions import (
    AUTH_PASSWORD_CHANGED,
    ENTITY_USER,
    USER_ACTIVATED,
    USER_CREATED,
    USER_DEACTIVATED,
    USER_ROLE_CHANGED,
    USER_UPDATED,
)
from app.core.auth_errors import AppException, AuthError, ErrorDef
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.org_roles import CLIENT, OWNER, TEAM_MANAGER
from app.core.security import hash_password, validate_password_strength, verify_password
from app.core.tenant import TenantContext, get_tenant_context, require_org_admin, require_org_owner
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.task_request_repository import TaskRequestRepository
from app.repositories.team_repository import TeamRepository
from app.repositories.user_repository import UserRepository
from app.schemas.task_request import ClientTaskRequestOut
from app.schemas.user import ChangePasswordRequest, SelfProfileUpdate, UserCreate, UserRead, UserUpdate
from app.services import activity_service, avatar_upload_service, logo_upload_service

router = APIRouter(prefix="/users", tags=["Users"])

_USER_NOT_FOUND = ErrorDef(code="USER_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="User not found.")
_EMAIL_EXISTS = ErrorDef(code="EMAIL_EXISTS", status=http_status.HTTP_409_CONFLICT, message="A user with this email already exists.")
_CANNOT_DELETE_SELF = ErrorDef(code="CANNOT_DELETE_SELF", status=http_status.HTTP_400_BAD_REQUEST, message="You cannot delete your own account.")
_CANNOT_CHANGE_OWNER_ROLE = ErrorDef(code="CANNOT_CHANGE_OWNER_ROLE", status=http_status.HTTP_400_BAD_REQUEST, message="The organization owner's role cannot be changed. Transfer ownership first.")
_INVALID_CURRENT_PASSWORD = ErrorDef(code="INVALID_CURRENT_PASSWORD", status=http_status.HTTP_400_BAD_REQUEST, message="Current password is incorrect.")
_NOT_A_CLIENT = ErrorDef(code="USER_NOT_A_CLIENT", status=http_status.HTTP_400_BAD_REQUEST, message="This user is not a client.")


def _still_manages_teams_error(team_names: list[str]) -> ErrorDef:
    names = ", ".join(team_names)
    return ErrorDef(
        code="STILL_MANAGES_TEAMS",
        status=http_status.HTTP_400_BAD_REQUEST,
        message=f"This user still manages the following team(s): {names}. Assign a different team manager to those teams first.",
    )


@router.get("/me", response_model=UserRead)
async def read_current_user(current_user: User = Depends(get_current_user)):
    return UserRead.model_validate(current_user)


@router.patch("/me", response_model=UserRead)
async def update_current_user(
    payload: SelfProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Self-service profile edit — full name and email only. Role/admin/etc.
    stay admin-only via `PATCH /users/{id}`."""
    user_repo = UserRepository(db)

    if payload.email and payload.email != current_user.email:
        if await user_repo.get_by_email(payload.email):
            raise AppException(_EMAIL_EXISTS)

    updated = await user_repo.update(current_user, payload)
    return UserRead.model_validate(updated)


@router.post("/me/change-password", status_code=http_status.HTTP_200_OK)
async def change_current_user_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    token_payload: dict = Depends(AccessTokenBearer()),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise AppException(_INVALID_CURRENT_PASSWORD)

    strength = validate_password_strength(payload.new_password)
    if not strength["valid"]:
        raise AuthError.invalid_password(strength["errors"])

    current_user.hashed_password = hash_password(payload.new_password)
    await db.commit()

    # Task #8B — `get_current_user` alone carries no org context, so
    # AccessTokenBearer() is added purely to read org_id off the same
    # token's own claims (same established pattern as auth.py's
    # logout/get_me). Never log the old/new password or its hash.
    raw_org_id = token_payload.get("org_id")
    if raw_org_id:
        await activity_service.record(
            db, organization_id=uuid.UUID(str(raw_org_id)), actor=current_user,
            action=AUTH_PASSWORD_CHANGED,
        )

    return {"message": "Password updated successfully."}


@router.post("/me/profile-picture", response_model=UserRead)
async def upload_current_user_profile_picture(
    file: UploadFile = FastAPIFile(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Self-service avatar upload. The target user comes only from the
    authenticated token (`get_current_user`) — there is no user_id in the
    request, so this can never be pointed at another account.

    Order matters here (see avatar_upload_service / logo_upload_service
    docs for the same pattern used by org/project logos): the new file is
    validated and written to disk *before* anything is committed, so a
    bad upload never touches the database, and the old file is only
    deleted *after* the new reference is safely committed, so a
    replacement can never leave the user without any avatar file on disk.
    If the commit itself fails, the newly-written file is removed instead
    of leaving an orphan.
    """
    new_url = await avatar_upload_service.save_avatar(file, current_user.id)

    previous_url = current_user.profile_picture_url
    try:
        current_user.profile_picture_url = new_url
        await db.commit()
    except Exception:
        await db.rollback()
        await logo_upload_service.delete_logo_file(new_url, avatar_upload_service.AVATAR_PREFIX)
        raise

    await logo_upload_service.delete_logo_file(previous_url, avatar_upload_service.AVATAR_PREFIX)
    await db.refresh(current_user)
    return UserRead.model_validate(current_user)


@router.delete("/me/profile-picture", response_model=UserRead)
async def delete_current_user_profile_picture(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clears the avatar reference and best-effort deletes the owned file.
    Never touches any other profile field."""
    previous_url = current_user.profile_picture_url
    current_user.profile_picture_url = None
    await db.commit()
    await db.refresh(current_user)

    await logo_upload_service.delete_logo_file(previous_url, avatar_upload_service.AVATAR_PREFIX)
    return UserRead.model_validate(current_user)


@router.get("", response_model=list[UserRead])
async def list_users(
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all active users in the current organization."""
    from app.repositories.organization_repository import OrganizationRepository
    org_repo = OrganizationRepository(db)
    members = await org_repo.list_members(tenant.organization_id)
    return [
        UserRead.model_validate(user).model_copy(update={
            "role": membership.role,
            "is_org_admin": membership.is_org_admin,
            "is_team_manager": membership.is_team_manager,
            "is_project_manager": membership.is_project_manager,
        })
        for membership, user in members
    ]


@router.get("/{user_id}/task-requests", response_model=list[ClientTaskRequestOut])
async def list_client_task_requests(
    user_id: int,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """A client's task requests across every project they belong to — the
    "normal list" shown in place of a scoreboard on the Users page, since
    clients (unlike staff) don't have one."""
    org_repo = OrganizationRepository(db)
    membership = await org_repo.get_membership(tenant.organization_id, user_id)
    if membership is None:
        raise AppException(_USER_NOT_FOUND)
    if membership.role != CLIENT:
        raise AppException(_NOT_A_CLIENT)

    repo = TaskRequestRepository(db, tenant.organization_id)
    requests = await repo.list_for_client(user_id)
    return [
        ClientTaskRequestOut.model_validate(r).model_copy(
            update={"project_name": r.project.name if r.project else None}
        )
        for r in requests
    ]


@router.post("", response_model=UserRead, status_code=http_status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user and automatically add them to the current organization."""
    user_repo = UserRepository(db)

    existing = await user_repo.get_by_email(payload.email)
    if existing:
        raise AppException(_EMAIL_EXISTS)

    user = await user_repo.create(payload)

    # Add user to this organization with the role specified in the request.
    from app.repositories.organization_repository import OrganizationRepository
    org_repo = OrganizationRepository(db)
    await org_repo.add_member(tenant.organization_id, user.id, payload.role)
    await db.commit()
    await db.refresh(user)

    # Task #8B — this is a direct admin-created account (no
    # OrganizationInvitation row, no email sent), distinct from
    # invite_member()'s user.invited flow — "invitation sent" and "user
    # created" are deliberately two different events for two different
    # workflows, never conflated.
    await activity_service.record(
        db, organization_id=tenant.organization_id, actor=tenant.user,
        action=USER_CREATED, entity_type=ENTITY_USER, entity_id=user.id, entity_label=user.full_name,
        metadata={"role": payload.role},
    )

    return UserRead.model_validate(user).model_copy(update={"role": payload.role})


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(user_id)
    if user is None:
        raise AppException(_USER_NOT_FOUND)

    if payload.email and payload.email.lower().strip() != user.email:
        if await user_repo.get_by_email(payload.email):
            raise AppException(_EMAIL_EXISTS)

    org_repo = OrganizationRepository(db)
    membership = await org_repo.get_membership(tenant.organization_id, user_id)
    current_role = membership.role if membership else user.role
    current_is_team_manager = membership.is_team_manager if membership else False
    current_is_active = user.is_active

    if payload.role is not None:
        is_owner_record = user_id == tenant.organization.owner_id
        if is_owner_record and payload.role != OWNER:
            raise AppException(_CANNOT_CHANGE_OWNER_ROLE)
        if not is_owner_record and payload.role == OWNER:
            raise AppException(_CANNOT_CHANGE_OWNER_ROLE)

    # Will this user still hold team-manager capability (functional role or
    # additive flag) once this update is applied? If not, and they still
    # manage team(s) via team_manager_id, block it — that FK is required and
    # would otherwise be left pointing at a non-manager.
    next_role = payload.role if payload.role is not None else current_role
    next_is_team_manager = payload.is_team_manager if payload.is_team_manager is not None else current_is_team_manager
    still_has_team_manager_capability = next_role == TEAM_MANAGER or next_is_team_manager
    losing_team_manager_capability = (
        (current_role == TEAM_MANAGER or current_is_team_manager) and not still_has_team_manager_capability
    )
    if losing_team_manager_capability:
        team_repo = TeamRepository(db, tenant.organization_id)
        managed_teams = await team_repo.list_for_manager(user_id)
        if managed_teams:
            raise AppException(_still_manages_teams_error([t.name for t in managed_teams]))

    updated = await user_repo.update(user, payload)

    if membership and payload.role is not None:
        await org_repo.update_member_role(membership, payload.role)
        await db.commit()
    if membership and payload.is_org_admin is not None:
        await org_repo.update_member_admin_flag(membership, payload.is_org_admin)
        await db.commit()
    if membership and payload.is_team_manager is not None:
        await org_repo.update_member_team_manager_flag(membership, payload.is_team_manager)
        await db.commit()
    if membership and payload.is_project_manager is not None:
        await org_repo.update_member_project_manager_flag(membership, payload.is_project_manager)
        await db.commit()
    effective_role = membership.role if membership else updated.role
    effective_is_org_admin = membership.is_org_admin if membership else False
    effective_is_team_manager = membership.is_team_manager if membership else False
    effective_is_project_manager = membership.is_project_manager if membership else False

    if payload.role is not None and payload.role != current_role:
        await activity_service.record(
            db, organization_id=tenant.organization_id, actor=tenant.user,
            action=USER_ROLE_CHANGED, entity_type=ENTITY_USER, entity_id=user.id, entity_label=user.full_name,
            metadata={"from_role": current_role, "to_role": payload.role},
        )

    # Task #8B — is_active toggles get their own dedicated
    # user.activated/user.deactivated event instead of being folded into
    # the generic "fields_changed" list below, since active/inactive is
    # an important, distinct admin action worth its own audit entry.
    if payload.is_active is not None and payload.is_active != current_is_active:
        await activity_service.record(
            db, organization_id=tenant.organization_id, actor=tenant.user,
            action=USER_ACTIVATED if payload.is_active else USER_DEACTIVATED,
            entity_type=ENTITY_USER, entity_id=user.id, entity_label=user.full_name,
        )

    other_fields_changed = [
        field for field in ("full_name", "email", "is_org_admin", "is_team_manager", "is_project_manager")
        if getattr(payload, field, None) is not None
    ]
    if other_fields_changed:
        await activity_service.record(
            db, organization_id=tenant.organization_id, actor=tenant.user,
            action=USER_UPDATED, entity_type=ENTITY_USER, entity_id=user.id, entity_label=user.full_name,
            metadata={"fields_changed": other_fields_changed},
        )

    return UserRead.model_validate(updated).model_copy(update={
        "role": effective_role,
        "is_org_admin": effective_is_org_admin,
        "is_team_manager": effective_is_team_manager,
        "is_project_manager": effective_is_project_manager,
    })


@router.delete("/{user_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    tenant: TenantContext = Depends(require_org_owner),
    db: AsyncSession = Depends(get_db),
):
    if user_id == tenant.user.id:
        raise AppException(_CANNOT_DELETE_SELF)

    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(user_id)
    if user is None:
        raise AppException(_USER_NOT_FOUND)

    await user_repo.delete(user)
    return None
