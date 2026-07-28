"""Seed dummy user accounts into an existing organization for testing.

Usage (run from the backend/ directory with the venv active):

    python seed_dummy_users.py --org default --count 10
    python seed_dummy_users.py --org admin@ba3digitalmarketing.com --count 5 --team-id 1
    python seed_dummy_users.py --org default --reset          # remove previously-seeded dummy users
    python seed_dummy_users.py --org default --count 20 --role team_manager --prefix qa

Every seeded account:
- logs in immediately with `--password` (default below) — no OTP/email-verification step,
  matching how `seed_admin.py` activates the default admin account.
- is added as an `OrganizationMembership` with `--role` in the target org.
- is optionally added as a `TeamMembership` of `--team-id` (must belong to the same org).

Seeded emails follow `{prefix}{n}@dummyusers.com` (e.g. `dummy1@dummyusers.com`) so `--reset`
can find and remove exactly the accounts this script created, without touching real users.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.org_roles import ALL_ORG_ROLES, TEAM_MEMBER
from app.core.security import hash_password
from app.models.organization import Organization, OrganizationMembership
from app.models.team import Team, TeamMembership
from app.models.user import User

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed_dummy_users")

DEFAULT_PASSWORD = "Passw0rd!123"
DEFAULT_PREFIX = "dummy"


async def _resolve_organization(db, org_arg: str | None) -> Organization:
    if org_arg:
        result = await db.execute(select(Organization).where(Organization.slug == org_arg))
        org = result.scalar_one_or_none()
        if org is not None:
            return org

        result = await db.execute(select(User).where(User.email == org_arg.lower().strip()))
        user = result.scalar_one_or_none()
        if user is not None:
            result = await db.execute(
                select(Organization)
                .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
                .where(OrganizationMembership.user_id == user.id)
            )
            org = result.scalars().first()
            if org is not None:
                return org

        logger.error("No organization found matching slug or member email %r.", org_arg)
        sys.exit(1)

    result = await db.execute(select(Organization))
    orgs = result.scalars().all()
    if len(orgs) == 1:
        return orgs[0]

    logger.error("Multiple organizations exist — pass --org <slug-or-member-email> to pick one:")
    for o in orgs:
        logger.error("  - %s (slug=%s)", o.name, o.slug)
    sys.exit(1)


async def _resolve_team(db, team_id: int | None, org: Organization) -> Team | None:
    if team_id is None:
        return None
    team = await db.get(Team, team_id)
    if team is None or team.organization_id != org.id:
        logger.error("Team %s not found in organization %r.", team_id, org.name)
        sys.exit(1)
    return team


async def reset_dummy_users(db, org: Organization, prefix: str) -> None:
    pattern = f"{prefix}%@dummyusers.com"
    result = await db.execute(
        select(User)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(OrganizationMembership.organization_id == org.id, User.email.like(pattern))
    )
    users = result.scalars().unique().all()
    if not users:
        logger.info("No dummy users found to remove (pattern=%s).", pattern)
        return

    removed = 0
    for user in users:
        email = user.email
        try:
            await db.delete(user)
            await db.commit()  # commit per-user: a later failure must not roll back earlier deletes
            removed += 1
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup, keep going
            await db.rollback()
            logger.warning("Could not remove %s: %s", email, exc)

    logger.info("Removed %d dummy user(s).", removed)


async def seed_dummy_users(
    org_arg: str | None,
    count: int,
    role: str,
    team_id: int | None,
    prefix: str,
    password: str,
    reset: bool,
) -> None:
    async with AsyncSessionLocal() as db:
        org = await _resolve_organization(db, org_arg)
        logger.info("Target organization: %s (slug=%s)", org.name, org.slug)

        if reset:
            await reset_dummy_users(db, org, prefix)
            return

        team = await _resolve_team(db, team_id, org)
        if team is not None:
            logger.info("Assigning seeded users to team: %s", team.name)

        created = []
        skipped = []
        now = datetime.now(timezone.utc)

        for i in range(1, count + 1):
            email = f"{prefix}{i}@dummyusers.com"

            result = await db.execute(select(User).where(User.email == email))
            existing = result.scalar_one_or_none()
            if existing is not None:
                skipped.append(email)
                user = existing
            else:
                user = User(
                    full_name=f"{prefix.capitalize()} User {i}",
                    email=email,
                    hashed_password=hash_password(password),
                    role=role,
                    is_active=True,
                    email_verified_at=now,
                    last_login_otp_verified_at=now,
                )
                db.add(user)
                await db.flush()
                created.append(email)

            membership_result = await db.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == org.id,
                    OrganizationMembership.user_id == user.id,
                )
            )
            if membership_result.scalar_one_or_none() is None:
                db.add(OrganizationMembership(
                    organization_id=org.id, user_id=user.id, role=role, is_active=True,
                ))

            if team is not None:
                tm_result = await db.execute(
                    select(TeamMembership).where(
                        TeamMembership.team_id == team.id, TeamMembership.user_id == user.id,
                    )
                )
                if tm_result.scalar_one_or_none() is None:
                    db.add(TeamMembership(team_id=team.id, user_id=user.id))

        await db.commit()

        logger.info("")
        logger.info("Created %d new user(s), %d already existed.", len(created), len(skipped))
        if created:
            logger.info("Password for all seeded accounts: %s", password)
            logger.info("Sample login: %s / %s", created[0], password)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--org", help="Organization slug, or the email of an existing member of the target org. Required if more than one org exists.")
    parser.add_argument("--count", type=int, default=10, help="Number of dummy users to create (default: 10).")
    parser.add_argument("--role", default=TEAM_MEMBER, choices=sorted(ALL_ORG_ROLES), help="Org role to assign (default: team_member).")
    parser.add_argument("--team-id", type=int, default=None, help="Also add seeded users as members of this team.")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"Email/name prefix (default: {DEFAULT_PREFIX}). Also used by --reset to find accounts to remove.")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Password for all seeded accounts.")
    parser.add_argument("--reset", action="store_true", help="Delete previously-seeded dummy users (matching --prefix) instead of creating any.")
    args = parser.parse_args()

    asyncio.run(seed_dummy_users(
        org_arg=args.org, count=args.count, role=args.role, team_id=args.team_id,
        prefix=args.prefix, password=args.password, reset=args.reset,
    ))


if __name__ == "__main__":
    main()
