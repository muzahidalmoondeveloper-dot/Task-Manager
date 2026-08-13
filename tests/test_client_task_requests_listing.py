"""Regression test for the Users page's client detail view: clicking a
client should show a plain list of their task requests (across all their
projects) rather than a scoreboard — clients never have one.

Covers:
- GET /users/{id}/task-requests returns the client's requests with the
  project name attached, for an Owner/Admin caller.
- The same endpoint 400s with USER_NOT_A_CLIENT for a staff target.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.users import list_client_task_requests
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task_request import TaskRequest
from app.models.user import User


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="ClientReqs Owner", email=f"clientreqs.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        client_user = User(full_name="ClientReqs Client", email=f"clientreqs.client.{suffix}@test.invalid", hashed_password="x", role=CLIENT)
        staff_user = User(full_name="ClientReqs Staff", email=f"clientreqs.staff.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, client_user, staff_user])
        await db.flush()

        org = Organization(name=f"ClientReqs Org {suffix}", slug=f"clientreqs-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        client_membership = OrganizationMembership(organization_id=org.id, user_id=client_user.id, role=CLIENT)
        staff_membership = OrganizationMembership(organization_id=org.id, user_id=staff_user.id, role="team_member")
        db.add_all([owner_membership, client_membership, staff_membership])
        await db.commit()

        project = Project(name=f"ClientReqs Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=client_user.id))
        await db.commit()

        request = TaskRequest(organization_id=org.id, project_id=project.id, submitted_by_id=client_user.id, title="Please add a login page")
        db.add(request)
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)

        try:
            results = await list_client_task_requests(client_user.id, tenant=owner_tenant, db=db)
            assert len(results) == 1, "the owner must see the client's one submitted request"
            assert results[0].title == "Please add a login page"
            assert results[0].project_name == project.name, "the project name must be attached for cross-project display"

            try:
                await list_client_task_requests(staff_user.id, tenant=owner_tenant, db=db)
                raise AssertionError("USER_NOT_A_CLIENT must be raised when the target isn't a client")
            except AppException as exc:
                assert exc.code == "USER_NOT_A_CLIENT"
                assert exc.status_code == 400

        finally:
            await db.execute(delete(TaskRequest).where(TaskRequest.id == request.id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, client_user.id, staff_user.id])))
            await db.commit()

    await engine.dispose()


def test_owner_can_list_a_clients_task_requests_across_projects():
    asyncio.run(_scenario())
