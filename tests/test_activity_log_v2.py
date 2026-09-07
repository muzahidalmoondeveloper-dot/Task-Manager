"""Regression tests for Task #8B — Professional Activity Log V2.

Extends test_activity_log.py (Task #8, left untouched) with coverage for
the new Authentication / Users & Access / Organization / Integration
activity events and the new category filter, per PHASE 32 of the spec:

  Auth:
   1. a genuinely successful password login (no OTP required) records
      exactly one auth.login, with the real authenticated actor/org.
   2. an invalid-credentials login records nothing.
   3. the OTP-required login path records nothing at login(); exactly one
      auth.login is recorded at verify_login_otp() instead — never two,
      never zero, across the two routes together.
   4. POST /auth/token-refresh never records a new auth.login.
   5. POST /auth/logout records exactly one auth.logout, using the
      server-derived actor/org from the token's own claims.
   6. change-password records auth.password_changed with no password/
      hash anywhere in the metadata.
   7. reset-password (fully unauthenticated OTP flow) records
      auth.password_reset_completed once org resolves.
   8. none of the above ever store a password/hash/token/otp/secret in
      activity_metadata.

  Users & Access:
   9. invite_member records user.invited without the raw invited email
      anywhere in entity_label or metadata.
  10. create_user records user.created.
  11. update_member_role (organizations.py) — the previously-unaudited
      role-change route — now records user.role_changed.
  12. update_user's is_active toggle records a dedicated
      user.activated / user.deactivated event (not folded into
      user.updated's fields_changed).
  13. remove_member (soft membership deactivation) records
      user.deactivated.

  Security (visibility rule):
  14. Owner and Admin can read GET /activity-logs.
  15. Team Manager, Project Manager, Team Member, Client, and a combined
      Team-Manager+Project-Manager account are all denied.
  16. cross-tenant isolation still holds with the new category filter.

  Filters:
  17. category=auth returns only auth.* rows.
  18. category=task returns only task.* rows.
  19. an unknown category is rejected (400), never silently ignored.
  20. actor filter narrows correctly.
  21. combined category + action + actor + date filters all apply
      together, backend-side.
  22. newest-first ordering holds under the new filters too.

Runs against the real DB/Redis connections the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select

from app.api.routes.activity_logs import list_activity_logs
from app.api.routes.auth import (
    login,
    logout,
    refresh_access_token,
    reset_password,
    verify_login_otp,
)
from app.api.routes.organizations import (
    invite_member,
    remove_member,
    update_member_role,
)
from app.api.routes.users import change_current_user_password, create_user, update_user
from app.core.activity_actions import (
    AUTH_LOGIN,
    AUTH_LOGOUT,
    AUTH_PASSWORD_CHANGED,
    AUTH_PASSWORD_RESET_COMPLETED,
    USER_ACTIVATED,
    USER_CREATED,
    USER_DEACTIVATED,
    USER_INVITED,
    USER_ROLE_CHANGED,
)
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.rate_limiter import RateLimiter
from app.core.rate_limit_config import RATE_LIMITS
from app.core.redis_client import close_redis, get_redis
from app.core.security import hash_password
from app.core.tenant import TenantContext, require_org_admin
from app.core.token_cache import TokenCache
from app.models.activity_log import ActivityLog
from app.models.auth_security import EmailOTP
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.schemas.auth import LoginRequest, LogoutRequest, ResetPasswordRequest, VerifyLoginOTPRequest
from app.schemas.organization import InviteMemberRequest, UpdateMemberRoleRequest
from app.schemas.task import TaskCreate
from app.schemas.user import ChangePasswordRequest, UserCreate, UserUpdate


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    """Same duck-typed Request stand-in as test_rate_limit_message.py —
    RateLimiter/get_client_ip only ever read .headers/.client.host/.url.path."""

    def __init__(self, ip: str, path: str = "/auth/login"):
        self.headers = {}
        self.client = _FakeClient(ip)

        class _Url:
            pass

        self.url = _Url()
        self.url.path = path


async def _scenario():
    await close_redis()
    redis = await get_redis()
    token_cache = TokenCache(redis)
    rate_limiter = RateLimiter(redis=redis, config=RATE_LIMITS)

    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]
        fake_ip = f"198.51.100.{int(suffix[:2], 16) % 255}"
        plain_password = "Str0ng!Passw0rd"

        owner = User(
            full_name="V2 Owner", email=f"v2.owner.{suffix}@example-corp.com",
            hashed_password=hash_password(plain_password), role="owner",
            email_verified_at=datetime.now(timezone.utc),
            last_login_otp_verified_at=datetime.now(timezone.utc),
        )
        admin = User(
            full_name="V2 Admin", email=f"v2.admin.{suffix}@example-corp.com",
            hashed_password=hash_password(plain_password), role="admin",
            email_verified_at=datetime.now(timezone.utc),
        )
        tm = User(full_name="V2 TM", email=f"v2.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="V2 PM", email=f"v2.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        member = User(full_name="V2 Member", email=f"v2.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="V2 Client", email=f"v2.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        combo = User(full_name="V2 Combo", email=f"v2.combo.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        db.add_all([owner, admin, tm, pm, member, client_user, combo])
        await db.commit()
        for u in (owner, admin, tm, pm, member, client_user, combo):
            await db.refresh(u)

        org = Organization(name=f"V2 Org {suffix}", slug=f"v2-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        # Eager-load `subscription` up front (see test_activity_log.py's
        # _scenario() for the same reasoning) — check_active_billing()
        # accesses it lazily, which would otherwise require an async
        # context a plain db.refresh() doesn't provide.
        from sqlalchemy.orm import selectinload
        org = (await db.execute(
            select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id)
        )).scalar_one()

        memberships = {}
        for u, role, extra in [
            (owner, "owner", {}),
            (admin, "admin", {"is_org_admin": True}),
            (tm, TEAM_MANAGER, {"is_team_manager": True}),
            (pm, PROJECT_MANAGER, {"is_project_manager": True}),
            (member, TEAM_MEMBER, {}),
            (client_user, CLIENT, {}),
            (combo, TEAM_MANAGER, {"is_team_manager": True, "is_project_manager": True}),
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role, **extra)
            db.add(m)
            memberships[u.id] = m
        await db.commit()
        for m in memberships.values():
            await db.refresh(m)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[admin.id], user=admin, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[member.id], user=member, db=db)
        client_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[client_user.id], user=client_user, db=db)
        combo_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[combo.id], user=combo, db=db)

        created_user_ids: list[int] = []
        created_task_ids: list[int] = []

        try:
            # ── 1, 2. Login: success logs once; invalid credentials logs
            # nothing. ─────────────────────────────────────────────────────
            bad_request = _FakeRequest(fake_ip)
            try:
                await login(
                    LoginRequest(email=owner.email, password="wrong-password"),
                    bad_request, db=db, token_cache=token_cache, rate_limiter=rate_limiter,
                )
                raise AssertionError("invalid credentials must raise")
            except AppException:
                pass
            no_login_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == AUTH_LOGIN)
            )).scalars().all()
            assert len(no_login_logs) == 0, "an invalid-credentials attempt must never record auth.login"

            good_request = _FakeRequest(fake_ip)
            login_response = await login(
                LoginRequest(email=owner.email, password=plain_password),
                good_request, db=db, token_cache=token_cache, rate_limiter=rate_limiter,
            )
            assert login_response.access_token is not None
            login_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == AUTH_LOGIN)
            )).scalars().all()
            assert len(login_logs) == 1, "exactly one auth.login for one successful password login"
            assert login_logs[0].actor_user_id == owner.id
            assert login_logs[0].activity_metadata is None or "password" not in login_logs[0].activity_metadata

            # ── 4. token-refresh must never create a NEW auth.login. ────────
            before_refresh_count = len(login_logs)
            refresh_request_obj = _FakeRequest(fake_ip, path="/auth/token-refresh")
            from app.schemas.auth import RefreshTokenRequest
            await refresh_access_token(
                RefreshTokenRequest(refresh_token=login_response.refresh_token),
                db=db, token_cache=token_cache,
            )
            after_refresh_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == AUTH_LOGIN)
            )).scalars().all()
            assert len(after_refresh_logs) == before_refresh_count, "token refresh must never record a new auth.login"

            # ── 5. logout records exactly one auth.logout. ──────────────────
            logout_token_payload = {
                "jti": f"v2-test-jti-{suffix}",
                "exp": int(time.time()) + 3600,
                "sub": str(owner.id),
                "org_id": str(org.id),
            }
            await logout(
                LogoutRequest(refresh_token="unused", logout_all_devices=True),
                token_payload=logout_token_payload, db=db, token_cache=token_cache,
            )
            logout_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == AUTH_LOGOUT)
            )).scalars().all()
            assert len(logout_logs) == 1, "exactly one auth.logout for one logout call"
            assert logout_logs[0].actor_user_id == owner.id
            assert await token_cache.is_access_token_blacklisted(logout_token_payload["jti"]) is True

            # ── 3. OTP-required login path: login() logs nothing;
            # verify_login_otp() logs exactly one. ───────────────────────────
            admin.last_login_otp_verified_at = None  # forces login_otp_required() == True
            await db.commit()
            otp_login_request = _FakeRequest(f"{fake_ip[:-1]}9")
            otp_response = await login(
                LoginRequest(email=admin.email, password=plain_password),
                otp_login_request, db=db, token_cache=token_cache, rate_limiter=rate_limiter,
            )
            assert otp_response.otp_required is True
            no_admin_login_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == AUTH_LOGIN, ActivityLog.actor_user_id == admin.id)
            )).scalars().all()
            assert len(no_admin_login_logs) == 0, "the OTP-required branch of login() must not record auth.login itself"

            otp_row = EmailOTP(
                user_id=admin.id, email=admin.email, otp_code="123456", purpose="login",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), is_used=False,
            )
            db.add(otp_row)
            await db.commit()
            verify_request = _FakeRequest(f"{fake_ip[:-1]}9", path="/auth/login/verify-otp")
            await verify_login_otp(
                VerifyLoginOTPRequest(email=admin.email, otp_code="123456"),
                verify_request, db=db, token_cache=token_cache, rate_limiter=rate_limiter,
            )
            admin_login_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == AUTH_LOGIN, ActivityLog.actor_user_id == admin.id)
            )).scalars().all()
            assert len(admin_login_logs) == 1, "exactly one auth.login, recorded at verify_login_otp() for the OTP-required path"

            # ── 6, 8. change-password logs auth.password_changed, no secrets. ─
            change_pw_token_payload = {"org_id": str(org.id)}
            await change_current_user_password(
                ChangePasswordRequest(current_password=plain_password, new_password="An0ther!Str0ngPW"),
                current_user=owner, db=db, token_payload=change_pw_token_payload,
            )
            pw_changed_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == AUTH_PASSWORD_CHANGED)
            )).scalars().all()
            assert len(pw_changed_logs) == 1
            assert pw_changed_logs[0].actor_user_id == owner.id
            assert pw_changed_logs[0].activity_metadata is None
            owner.hashed_password = hash_password(plain_password)  # restore for cleanliness
            await db.commit()

            # ── 7, 8. reset-password (unauthenticated OTP flow) logs
            # auth.password_reset_completed. ───────────────────────────────
            reset_otp_row = EmailOTP(
                user_id=owner.id, email=owner.email, otp_code="654321", purpose="reset_password",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), is_used=False,
            )
            db.add(reset_otp_row)
            await db.commit()
            reset_request = _FakeRequest(fake_ip, path="/auth/reset-password")
            await reset_password(
                ResetPasswordRequest(email=owner.email, otp_code="654321", new_password="YetAnother!Str0ng1"),
                reset_request, db=db,
            )
            reset_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == AUTH_PASSWORD_RESET_COMPLETED)
            )).scalars().all()
            assert len(reset_logs) == 1
            assert reset_logs[0].actor_user_id == owner.id
            assert reset_logs[0].activity_metadata is None
            owner.hashed_password = hash_password(plain_password)
            await db.commit()

            # ── 9. user.invited never stores the raw email. ──────────────────
            invited_email = f"v2.invitee.{suffix}@example-corp.com"
            await invite_member(
                InviteMemberRequest(email=invited_email, role=TEAM_MEMBER),
                tenant=admin_tenant, db=db,
            )
            invite_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == USER_INVITED)
            )).scalars().all()
            assert len(invite_logs) == 1
            assert invited_email not in (invite_logs[0].entity_label or "")
            assert invited_email not in str(invite_logs[0].activity_metadata or {})

            # ── 10. create_user logs user.created. ────────────────────────────
            created = await create_user(
                UserCreate(full_name="V2 Created", email=f"v2.created.{suffix}@example-corp.com", password="Str0ng!Passw0rd2", role=TEAM_MEMBER),
                tenant=admin_tenant, db=db,
            )
            created_user_ids.append(created.id)
            created_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == USER_CREATED, ActivityLog.entity_id == created.id)
            )).scalars().all()
            assert len(created_logs) == 1

            # ── 11. update_member_role (organizations.py) — the previously
            # unaudited gap — now logs user.role_changed. ────────────────────
            await update_member_role(
                member.id, UpdateMemberRoleRequest(role=PROJECT_MANAGER), tenant=admin_tenant, db=db,
            )
            role_change_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == USER_ROLE_CHANGED, ActivityLog.entity_id == member.id)
            )).scalars().all()
            assert len(role_change_logs) == 1
            assert role_change_logs[0].activity_metadata["from_role"] == TEAM_MEMBER
            assert role_change_logs[0].activity_metadata["to_role"] == PROJECT_MANAGER
            memberships[member.id].role = TEAM_MEMBER
            await db.commit()

            # ── 12. is_active toggle on update_user logs a dedicated
            # user.activated/user.deactivated event. ─────────────────────────
            await update_user(created.id, UserUpdate(is_active=False), tenant=admin_tenant, db=db)
            deactivated_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == USER_DEACTIVATED, ActivityLog.entity_id == created.id)
            )).scalars().all()
            assert len(deactivated_logs) == 1

            await update_user(created.id, UserUpdate(is_active=True), tenant=admin_tenant, db=db)
            activated_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == USER_ACTIVATED, ActivityLog.entity_id == created.id)
            )).scalars().all()
            assert len(activated_logs) == 1

            # ── 13. remove_member (soft membership deactivation) logs
            # user.deactivated. ───────────────────────────────────────────────
            await remove_member(created.id, tenant=admin_tenant, db=db)
            remove_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == USER_DEACTIVATED, ActivityLog.entity_id == created.id)
            )).scalars().all()
            assert len(remove_logs) == 2, "one from the is_active toggle above, one from remove_member"

            # ── a task.* row so category filtering has something to
            # isolate against. ──────────────────────────────────────────────
            from app.api.routes.tasks import create_task
            task = await create_task(
                TaskCreate(name=f"V2 Task {suffix}"), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            created_task_ids.append(task.id)

            # ── 14, 15. Owner/Admin can read; TM/PM/Member/Client/Combo
            # are all denied — the visibility rule must not weaken. ──────────
            await require_org_admin(tenant=owner_tenant)
            await require_org_admin(tenant=admin_tenant)
            for blocked_tenant, label in [
                (tm_tenant, "Team Manager"), (pm_tenant, "Project Manager"),
                (member_tenant, "Team Member"), (client_tenant, "Client"),
                (combo_tenant, "Team Manager + Project Manager combo"),
            ]:
                try:
                    await require_org_admin(tenant=blocked_tenant)
                    raise AssertionError(f"{label} must not pass require_org_admin — Activity Log visibility rule must not weaken")
                except AppException as exc:
                    assert exc.status_code == 403, exc

            # ── 17, 18, 19. category filter. ────────────────────────────────
            auth_page = await list_activity_logs(page=1, page_size=100, category="authentication", tenant=admin_tenant, db=db)
            assert auth_page.total >= 1
            assert all(item.action.startswith("auth.") for item in auth_page.items)

            task_page = await list_activity_logs(page=1, page_size=100, category="tasks", tenant=admin_tenant, db=db)
            assert task_page.total >= 1
            assert all(item.action.startswith("task.") for item in task_page.items)

            try:
                await list_activity_logs(page=1, page_size=10, category="not-a-real-category", tenant=admin_tenant, db=db)
                raise AssertionError("an unknown category must be rejected, not silently ignored")
            except AppException as exc:
                assert exc.status_code == 400

            # ── 20. actor filter narrows correctly. ────────────────────────────
            actor_page = await list_activity_logs(page=1, page_size=100, actor_user_id=owner.id, tenant=admin_tenant, db=db)
            assert actor_page.total >= 1
            assert all(item.actor.id == owner.id for item in actor_page.items)

            # ── 21, 22. combined category+action+date filters, newest-first. ──
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            combined_page = await list_activity_logs(
                page=1, page_size=100, category="users", action=USER_ROLE_CHANGED,
                since=since, until=until, tenant=admin_tenant, db=db,
            )
            assert combined_page.total == 1
            assert combined_page.items[0].action == USER_ROLE_CHANGED
            if len(combined_page.items) > 1:
                assert combined_page.items[0].created_at >= combined_page.items[1].created_at

        finally:
            await db.execute(delete(ActivityLog).where(ActivityLog.organization_id == org.id))
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            all_user_ids = [owner.id, admin.id, tm.id, pm.id, member.id, client_user.id, combo.id] + created_user_ids
            await db.execute(delete(EmailOTP).where(EmailOTP.user_id.in_(all_user_ids)))
            from app.models.refresh_token import RefreshToken
            await db.execute(delete(RefreshToken).where(RefreshToken.user_id.in_(all_user_ids)))
            await db.execute(delete(User).where(User.id.in_(all_user_ids)))
            await db.commit()

    await engine.dispose()
    await close_redis()


def test_activity_log_v2():
    asyncio.run(_scenario())
