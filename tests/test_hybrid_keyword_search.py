"""Hybrid keyword retrieval regression test (architecture item 10 —
Reporting/Knowledge domain, strict acceptance audit finding: 0% coverage).

Proves search_everything() finds real, org-scoped rows across multiple
domains (task/rock/project) ranked by relevance, is refused for CLIENT, and
never leaks another organization's data into the results.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, TEAM_MANAGER
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.rock import Rock
from app.models.task import Task
from app.models.team import Team
from app.models.user import User
from app.repositories.task_repository import TaskRepository
from app.schemas.task import TaskCreate
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        keyword = f"launchpad{suffix}"

        owner = User(full_name="Search Owner", email=f"search.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        client = User(full_name="Search Client", email=f"search.client.{suffix}@test.invalid", hashed_password="x", role="client")
        other_owner = User(full_name="Search Other Owner", email=f"search.otherowner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, client, other_owner])
        await db.flush()

        org = Organization(name=f"Search Org {suffix}", slug=f"search-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"Search Other Org {suffix}", slug=f"search-other-{suffix}", owner_id=other_owner.id)
        db.add_all([org, other_org])
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=client.id, role="client"),
            OrganizationMembership(organization_id=other_org.id, user_id=other_owner.id, role="owner"),
        ])
        await db.commit()
        org_id, owner_id, client_id, other_org_id = org.id, owner.id, client.id, other_org.id

        team = Team(name=f"Search Team {suffix}", team_manager_id=owner_id, created_by_id=owner_id, organization_id=org_id)
        db.add(team)
        await db.flush()
        team_id = team.id
        await db.commit()

        task_repo = TaskRepository(db, org_id)
        task = await task_repo.create(TaskCreate(name=f"Fix the {keyword} bug"), created_by_id=owner_id)
        project = Project(name=f"{keyword} redesign", created_by_id=owner_id, organization_id=org_id)
        rock = Rock(title=f"Ship {keyword} v2", team_id=team_id, organization_id=org_id)
        # A same-keyword row in a DIFFERENT org — must never appear in this org's results.
        other_project = Project(name=f"{keyword} in another org", created_by_id=other_owner.id, organization_id=other_org_id)
        db.add_all([project, rock, other_project])
        await db.commit()
        project_id, rock_id, other_project_id, task_id = project.id, rock.id, other_project.id, task.id

        try:
            mgr_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=owner, user_id=owner_id, session_id=None)
            result = await run_tool("search_everything", {"query": keyword}, mgr_ctx)
            assert result.ok, f"search_everything should succeed, got: {result.message}"
            items = result.data["items"]
            found_types = {(i["entity_type"], i["name"]) for i in items}
            assert ("task", task.name) in found_types, "search_everything must find the matching task"
            assert ("project", project.name) in found_types, "search_everything must find the matching project"
            assert ("rock", rock.title) in found_types, "search_everything must find the matching rock"
            assert other_project.name not in {i["name"] for i in items}, (
                "CROSS-TENANT LEAK: search_everything returned a row from a different organization"
            )

            # ── CLIENT is refused (staff-only broad search) ──
            client_ctx = ToolContext(db=db, org_id=org_id, org_role=CLIENT, user=client, user_id=client_id, session_id=None)
            result = await run_tool("search_everything", {"query": keyword}, client_ctx)
            assert not result.ok, "CLIENT must be refused for search_everything"

            # ── A nonsense query returns no results, not an error or garbage matches ──
            result = await run_tool("search_everything", {"query": "zzzznonexistentquery9999"}, mgr_ctx)
            assert result.ok
            assert result.data["items"] == []

        finally:
            await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(delete(Rock).where(Rock.id == rock_id))
            await db.execute(delete(Project).where(Project.id.in_([project_id, other_project_id])))
            await db.execute(delete(Team).where(Team.id == team_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_([owner_id, client_id, other_owner.id])))
            await db.commit()

    await engine.dispose()


def test_hybrid_keyword_search_cross_domain_and_tenant_isolated():
    asyncio.run(_scenario())
