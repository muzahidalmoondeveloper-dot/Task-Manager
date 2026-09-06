"""Single source of truth for "is this a legal Task/To-Do assignee"
(Task Assignee bug-fix follow-up) — the security boundary both
`POST /tasks` and `PATCH /tasks/{id}` call before persisting an
`assignee_id`, so the rule can never drift between create and update, and
can never be bypassed by a client that skips the frontend's own dropdown
filtering (frontend filtering is UX only; this is enforcement).

RULES (see the spec's "DEFINE THE AUTHORITATIVE ASSIGNEE RULES"):
  A. A Client (OrganizationMembership.role == CLIENT — the organization-
     scoped role is authoritative, never the legacy User.role column, see
     app.core.org_roles's own module docstring) may never be a Task
     assignee, regardless of who is assigning or which Task.
  B. If the task belongs to a Team (`team_id is not None`), the assignee
     must be a member of THAT exact team — `TeamRepository.has_access`,
     the same "manager or TeamMembership" rule already used for every
     other team-scoped permission check in this app (Rocks, Issues, KPIs,
     Team News, the team route itself). Admin/Owner power does not expand
     this set — see Rule C in the spec.
  F. If the task has no team_id, only the Client exclusion (Rule A) and
     plain organization-membership/active checks apply — no team-member
     restriction is invented where no team exists.

The assignee must also actually be an ACTIVE member of the caller's own
organization — `assignee_id` is never trusted to belong to the caller's
tenant just because it's a valid user id somewhere in the system (a user
id is global in this schema; organization membership is not).
"""

from uuid import UUID

from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import CLIENT
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.team_repository import TeamRepository
from app.repositories.user_repository import UserRepository

INVALID_ASSIGNEE = ErrorDef(
    code="INVALID_ASSIGNEE",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Selected assignee is not a valid member of this organization.",
)
CLIENT_NOT_ASSIGNABLE = ErrorDef(
    code="CLIENT_NOT_ASSIGNABLE",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="A Client cannot be assigned to a Task.",
)
ASSIGNEE_NOT_TEAM_MEMBER = ErrorDef(
    code="ASSIGNEE_NOT_TEAM_MEMBER",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Selected assignee is not a member of this Task's Team.",
)


async def validate_task_assignee(
    db: AsyncSession,
    *,
    organization_id: UUID,
    assignee_id: int | None,
    team_id: int | None,
) -> None:
    """Raises AppException (400) if `assignee_id` may not be assigned to a
    Task in this organization/team context. A `None` assignee_id (the
    Unassigned option, Rule E) is always valid and returns immediately —
    this function is never the thing that would force a Task to have an
    assignee."""
    if assignee_id is None:
        return

    org_repo = OrganizationRepository(db)
    membership = await org_repo.get_membership(organization_id, assignee_id)
    if membership is None or not membership.is_active:
        # Covers both a cross-tenant user id (exists globally, but not a
        # member of *this* organization) and a deactivated membership —
        # neither is a legal assignee here.
        raise AppException(INVALID_ASSIGNEE)

    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(assignee_id)
    if user is None or not user.is_active:
        raise AppException(INVALID_ASSIGNEE)

    # Rule A — Client is never Task-assignable, regardless of who is
    # assigning (Owner/Admin/Team Manager/Project Manager) or whether the
    # Client somehow also holds a TeamMembership row on this exact team
    # (a Client should never have one, but this check does not trust that
    # invariant — it rejects on role alone, independent of team membership).
    if membership.role == CLIENT:
        raise AppException(CLIENT_NOT_ASSIGNABLE)

    # Rule B/C — a Team Task's assignee must belong to THAT team. Admin/
    # Owner privilege lets them edit the task, not expand its valid
    # assignee set (Rule C). Uses the same has_access() the rest of the
    # app already treats as "belongs to this team" (manager OR
    # TeamMembership) — never a second, inconsistent definition.
    if team_id is not None:
        team_repo = TeamRepository(db, organization_id)
        if not await team_repo.has_access(team_id, assignee_id):
            raise AppException(ASSIGNEE_NOT_TEAM_MEMBER)
