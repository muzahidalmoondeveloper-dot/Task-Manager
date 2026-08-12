"""Regression test: "A Team Manager will have full access to everything
under their team, including the ability to create, edit, delete, and view
all team-related items" — and, by the same rule, is blocked from another
team's items.

BUG BEING GUARDED AGAINST: Rocks, Issues, and Team News had NO team-scope
check at all — any authenticated org member, on any team, could list,
create, update, or delete another team's items just by knowing its
team_id. KPIs were partially checked (create/update/delete required
Owner/Admin/Team Manager) but never verified the actor actually manages
THIS team, so any Team Manager could touch any team's KPIs. All four now
go through the same app.core.team_access.require_team_access() check
already proven for the team entity itself.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.issues import create_issue, list_issues
from app.api.routes.kpi import list_kpis
from app.api.routes.rocks import create_rock, list_rocks
from app.api.routes.team_news import create_news, list_news
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.issue import Issue
from app.models.organization import Organization, OrganizationMembership
from app.models.rock import Rock
from app.models.team import Team
from app.models.team_news import TeamNews
from app.models.user import User
from app.schemas.issue import IssueCreate
from app.schemas.rock import RockCreate
from app.schemas.team_news import NewsCreate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="TeamDomains Owner", email=f"teamdomains.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        tm = User(full_name="TeamDomains TM", email=f"teamdomains.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        db.add_all([owner, tm])
        await db.flush()

        org = Organization(name=f"TeamDomains Org {suffix}", slug=f"teamdomains-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER)
        db.add_all([owner_membership, tm_membership])
        await db.commit()
        await db.refresh(tm_membership)

        team_a = Team(name=f"Team A {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        team_b = Team(name=f"Team B {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_a, team_b])
        await db.commit()

        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=tm, db=db)

        rock_ids, issue_ids, news_ids = [], [], []
        try:
            # ── Full access to Team A (the team they manage): view + create ──
            assert await list_rocks(team_a.id, db=db, tenant=tm_tenant) == []
            rock = await create_rock(team_a.id, RockCreate(title=f"Rock A {suffix}"), db=db, tenant=tm_tenant)
            rock_ids.append(rock.id)
            assert rock.team_id == team_a.id

            assert await list_issues(team_a.id, timeframe=None, db=db, tenant=tm_tenant) == []
            issue = await create_issue(team_a.id, IssueCreate(title=f"Issue A {suffix}"), db=db, tenant=tm_tenant)
            issue_ids.append(issue.id)
            assert issue.team_id == team_a.id

            assert list(await list_news(team_a.id, db=db, tenant=tm_tenant)) == []
            news = await create_news(team_a.id, NewsCreate(title=f"News A {suffix}"), db=db, tenant=tm_tenant)
            news_ids.append(news.id)
            assert news.team_id == team_a.id

            assert await list_kpis(team_a.id, db=db, tenant=tm_tenant) == []

            # ── Blocked from Team B (not their team): view + create ──
            for coro in (
                list_rocks(team_b.id, db=db, tenant=tm_tenant),
                list_issues(team_b.id, timeframe=None, db=db, tenant=tm_tenant),
                list_news(team_b.id, db=db, tenant=tm_tenant),
                list_kpis(team_b.id, db=db, tenant=tm_tenant),
            ):
                try:
                    await coro
                    raise AssertionError("TEAM_NOT_ASSIGNED must be raised for a team this Team Manager doesn't manage")
                except AppException as exc:
                    assert exc.code == "TEAM_NOT_ASSIGNED"
                    assert exc.status_code == 403

            try:
                await create_rock(team_b.id, RockCreate(title="Should never exist"), db=db, tenant=tm_tenant)
                raise AssertionError("creating a rock under an unmanaged team must be forbidden")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            try:
                await create_news(team_b.id, NewsCreate(title="Should never exist"), db=db, tenant=tm_tenant)
                raise AssertionError("creating news under an unmanaged team must be forbidden")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

        finally:
            if rock_ids:
                await db.execute(delete(Rock).where(Rock.id.in_(rock_ids)))
            if issue_ids:
                await db.execute(delete(Issue).where(Issue.id.in_(issue_ids)))
            if news_ids:
                await db.execute(delete(TeamNews).where(TeamNews.id.in_(news_ids)))
            await db.execute(delete(Team).where(Team.id.in_([team_a.id, team_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id])))
            await db.commit()

    await engine.dispose()


def test_team_manager_has_full_access_to_own_team_domains_and_none_to_others():
    asyncio.run(_scenario())
