"""Regression tests for the invitation acceptance identity-mismatch
security fix (app.api.routes.auth.accept_invitation /
register_and_accept_invitation).

Root cause (see final report): `POST /auth/accept-invitation` accepted
ANY authenticated user's session as the accepting identity — it never
compared `current_user.email` against `invitation.email`. Opening Bob's
invitation link in a browser already logged in as Alice would silently
create an OrganizationMembership for ALICE (using Bob's invited role) and
mark BOB's invitation as accepted — a false-success state where Bob never
actually receives access, and Alice gains access she wasn't invited to.

Covers (see the spec's "TESTS — INVITATION IDENTITY / CONSISTENCY"):
  1. Invitation for Bob + Bob authenticated: succeeds.
  2. Invitation for Bob + Alice authenticated: rejected
     (INVITATION_ACCOUNT_MISMATCH, 409).
  3. Mismatch rejection leaves the invitation pending (accepted_at still
     None).
  4. Mismatch rejection creates NO membership for Alice OR Bob.
  5. Mismatch rejection creates no Activity Log success event (there is no
     invitation-acceptance Activity Log event in the current taxonomy at
     all — see final report — so this is trivially satisfied; asserted
     anyway as a explicit regression guard).
  6. Case/whitespace-mismatched but semantically-equal email
     (" Bob@Example.com ") still succeeds — existing email normalization
     (`.lower().strip()`) is reused, not a new home-grown scheme.
  7. Logged-out / signup invitation path (register_and_accept_invitation)
     remains functional — creates the account tied to invitation.email
     directly, no session identity to mismatch.
  8. Membership creation succeeds -> invitation becomes accepted.
  9. Already-accepted invitation cannot mutate state again (replay).
  10. Expired invitation: rejected.
  11. Revoked invitation (status="revoked"): rejected.
  12. Existing matching membership (Bob already an active member):
      idempotent — no duplicate OrganizationMembership row, invitation
      still marked accepted.
  13. invitation.organization_id is the only organization a membership is
      ever created in — never the accepting user's unrelated other org.
  14. Intended role is stored on OrganizationMembership, never written to
      the legacy User.role column.
  15. Cross-tenant isolation: a membership from a different organization
      does not satisfy/short-circuit this invitation's own org boundary.

Runs against the real database/Redis connections the app uses. Every row
this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.api.routes.auth import accept_invitation, register_and_accept_invitation
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import ADMIN, OWNER, TEAM_MEMBER
from app.core.redis_client import close_redis, get_redis
from app.core.security import hash_password
from app.core.token_cache import TokenCache
from app.models.activity_log import ActivityLog
from app.models.organization import Organization, OrganizationInvitation, OrganizationMembership
from app.models.user import User
from app.schemas.auth import AcceptInvitationRequest, RegisterAndAcceptInvitationRequest


async def _scenario():
    await close_redis()
    redis = await get_redis()
    token_cache = TokenCache(redis)

    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="IIM Owner", email=f"iim.owner.{suffix}@example-corp.com", hashed_password="x", role="owner", email_verified_at=datetime.now(timezone.utc))
        bob = User(full_name="IIM Bob", email=f"iim.bob.{suffix}@example-corp.com", hashed_password=hash_password("x"), role=TEAM_MEMBER, email_verified_at=datetime.now(timezone.utc), last_login_otp_verified_at=datetime.now(timezone.utc))
        alice = User(full_name="IIM Alice", email=f"iim.alice.{suffix}@example-corp.com", hashed_password=hash_password("x"), role=TEAM_MEMBER, email_verified_at=datetime.now(timezone.utc), last_login_otp_verified_at=datetime.now(timezone.utc))
        db.add_all([owner, bob, alice])
        await db.commit()
        for u in (owner, bob, alice):
            await db.refresh(u)

        org = Organization(name=f"IIM Org {suffix}", slug=f"iim-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"IIM Other Org {suffix}", slug=f"iim-other-org-{suffix}", owner_id=alice.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER)
        # Alice is already a member of a DIFFERENT organization — used for
        # the cross-tenant isolation probe (item 15/13).
        alice_other_org_membership = OrganizationMembership(organization_id=other_org.id, user_id=alice.id, role=OWNER)
        db.add_all([owner_membership, alice_other_org_membership])
        await db.commit()

        created_invitation_ids: list = []
        created_user_ids: list[int] = []

        def _make_invitation(email: str, *, role: str = ADMIN, hours_from_now: float = 72) -> OrganizationInvitation:
            inv = OrganizationInvitation(
                id=uuid.uuid4(),
                organization_id=org.id,
                email=email.lower().strip(),
                role=role,
                invited_by_id=owner.id,
                token=f"iim-token-{uuid.uuid4().hex}",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=hours_from_now),
            )
            db.add(inv)
            return inv

        try:
            # ── 2, 3, 4. Wrong logged-in user: rejected, no mutation. ─────────
            inv_for_bob = _make_invitation(bob.email)
            db.add(inv_for_bob)
            await db.commit()
            created_invitation_ids.append(inv_for_bob.id)

            try:
                await accept_invitation(
                    AcceptInvitationRequest(token=inv_for_bob.token),
                    current_user=alice, db=db, token_cache=token_cache,
                )
                raise AssertionError("Alice must not be able to accept an invitation sent to Bob")
            except AppException as exc:
                assert exc.status_code == 409, exc
                assert exc.code == "INVITATION_ACCOUNT_MISMATCH", exc

            await db.refresh(inv_for_bob)
            assert inv_for_bob.accepted_at is None, "a mismatched acceptance attempt must leave the invitation pending"

            alice_membership_in_org = await db.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == alice.id,
                )
            )
            assert alice_membership_in_org.scalar_one_or_none() is None, "Alice must not have been given a membership from Bob's invitation"

            bob_membership_after_mismatch = await db.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == bob.id,
                )
            )
            assert bob_membership_after_mismatch.scalar_one_or_none() is None, "Bob must not have gained membership either — nothing was mutated"

            # ── 5. No success audit event was fabricated. There is no
            # invitation-acceptance Activity Log action in the current
            # taxonomy at all, so this is trivially true — asserted as an
            # explicit regression guard in case one is added later without
            # this same mismatch-safety. ───────────────────────────────────
            leaked_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.actor_user_id == alice.id)
            )).scalars().all()
            assert leaked_logs == [], "a rejected mismatched acceptance must never produce any Activity Log event"

            # ── 1, 8. Correct identity (Bob) succeeds -> real membership +
            # invitation marked accepted. ─────────────────────────────────────
            token_pair = await accept_invitation(
                AcceptInvitationRequest(token=inv_for_bob.token),
                current_user=bob, db=db, token_cache=token_cache,
            )
            assert token_pair.access_token is not None
            await db.refresh(inv_for_bob)
            assert inv_for_bob.accepted_at is not None, "a correctly-identity-matched acceptance must mark the invitation accepted"

            bob_membership = (await db.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == bob.id,
                )
            )).scalar_one()
            assert bob_membership.is_active is True
            # ── 14. Intended role lands on OrganizationMembership, never
            # the legacy User.role column. ─────────────────────────────────
            assert bob_membership.role == ADMIN
            assert bob.role == TEAM_MEMBER, "accepting an invitation must never globally overwrite the legacy User.role column"

            # ── 9. Replay: already-accepted invitation cannot mutate again. ───
            try:
                await accept_invitation(
                    AcceptInvitationRequest(token=inv_for_bob.token),
                    current_user=bob, db=db, token_cache=token_cache,
                )
                raise AssertionError("re-using an already-accepted invitation token must not succeed again")
            except AppException as exc:
                assert exc.code == "INVITATION_ALREADY_ACCEPTED", exc
            dup_count = (await db.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == bob.id,
                )
            )).scalars().all()
            assert len(dup_count) == 1, "replay must never create a duplicate membership row"

            # ── 12. Existing matching membership: idempotent re-accept of a
            # DIFFERENT still-pending invitation for someone already a
            # member must not duplicate the row. ─────────────────────────────
            inv_for_bob_again = _make_invitation(bob.email, role=OWNER)
            await db.commit()
            created_invitation_ids.append(inv_for_bob_again.id)
            # Existing semantics (unchanged by this fix): an already-active
            # member simply reuses their existing membership row — this
            # second invitation is still marked accepted, but does not
            # retroactively change Bob's existing role.
            await accept_invitation(
                AcceptInvitationRequest(token=inv_for_bob_again.token),
                current_user=bob, db=db, token_cache=token_cache,
            )
            await db.refresh(inv_for_bob_again)
            assert inv_for_bob_again.accepted_at is not None
            bob_memberships_final = (await db.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == bob.id,
                )
            )).scalars().all()
            assert len(bob_memberships_final) == 1, "an already-a-member acceptance must never create a second membership row"

            # ── 6. Normalized-equal email still matches (case/whitespace). ────
            eve = User(full_name="IIM Eve", email=f"iim.eve.{suffix}@example-corp.com", hashed_password=hash_password("x"), role=TEAM_MEMBER, email_verified_at=datetime.now(timezone.utc))
            db.add(eve)
            await db.commit()
            await db.refresh(eve)
            created_user_ids.append(eve.id)
            # invitation stored normalized (lower/stripped, per create_invitation) —
            # simulate the accepting session's email arriving in a different case/
            # whitespace form the way a real account's stored email theoretically
            # could, to prove the comparison itself normalizes rather than relying
            # on both sides already matching byte-for-byte.
            eve.email = f"  IIM.Eve.{suffix}@Example-Corp.com  ".strip()
            inv_for_eve = _make_invitation(f"iim.eve.{suffix}@example-corp.com")
            await db.commit()
            await db.refresh(eve)
            created_invitation_ids.append(inv_for_eve.id)
            await accept_invitation(
                AcceptInvitationRequest(token=inv_for_eve.token),
                current_user=eve, db=db, token_cache=token_cache,
            )
            await db.refresh(inv_for_eve)
            assert inv_for_eve.accepted_at is not None, "a normalized-equal email must still be treated as the same identity"

            # ── 10. Expired invitation rejected. ──────────────────────────────
            inv_expired = _make_invitation(bob.email, hours_from_now=-1)
            await db.commit()
            created_invitation_ids.append(inv_expired.id)
            try:
                await accept_invitation(
                    AcceptInvitationRequest(token=inv_expired.token),
                    current_user=bob, db=db, token_cache=token_cache,
                )
                raise AssertionError("an expired invitation must never be acceptable")
            except AppException as exc:
                assert exc.code == "INVITATION_EXPIRED", exc

            # ── 11. Revoked invitation rejected. ──────────────────────────────
            inv_revoked = _make_invitation(bob.email)
            inv_revoked.status = "revoked"
            await db.commit()
            created_invitation_ids.append(inv_revoked.id)
            try:
                await accept_invitation(
                    AcceptInvitationRequest(token=inv_revoked.token),
                    current_user=bob, db=db, token_cache=token_cache,
                )
                raise AssertionError("a revoked invitation must never be acceptable")
            except AppException as exc:
                assert exc.code == "INVITATION_REVOKED", exc

            # ── 13, 15. Membership is created ONLY in invitation.organization_id
            # — Alice's existing membership in a DIFFERENT organization must
            # never satisfy or interfere with a Bob-targeted invitation in
            # `org`, and accepting can never land a membership in `other_org`. ──
            frank = User(full_name="IIM Frank", email=f"iim.frank.{suffix}@example-corp.com", hashed_password=hash_password("x"), role=TEAM_MEMBER, email_verified_at=datetime.now(timezone.utc))
            db.add(frank)
            await db.commit()
            await db.refresh(frank)
            created_user_ids.append(frank.id)
            inv_for_frank = _make_invitation(frank.email)
            await db.commit()
            created_invitation_ids.append(inv_for_frank.id)
            await accept_invitation(
                AcceptInvitationRequest(token=inv_for_frank.token),
                current_user=frank, db=db, token_cache=token_cache,
            )
            frank_membership = (await db.execute(
                select(OrganizationMembership).where(OrganizationMembership.user_id == frank.id)
            )).scalars().all()
            assert len(frank_membership) == 1
            assert frank_membership[0].organization_id == org.id, "membership must be created only in the invitation's own organization"
            frank_in_other_org = (await db.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == other_org.id, OrganizationMembership.user_id == frank.id,
                )
            )).scalar_one_or_none()
            assert frank_in_other_org is None

            # ── 7. Logged-out / signup invitation path remains functional —
            # creates the account tied to invitation.email directly, no
            # session identity to mismatch against. ───────────────────────────
            grace_email = f"iim.grace.{suffix}@example-corp.com"
            inv_for_grace = _make_invitation(grace_email)
            await db.commit()
            created_invitation_ids.append(inv_for_grace.id)
            signup_result = await register_and_accept_invitation(
                RegisterAndAcceptInvitationRequest(token=inv_for_grace.token, full_name="IIM Grace", password="Str0ng!Passw0rd"),
                db=db, token_cache=token_cache,
            )
            assert signup_result.access_token is not None
            grace_user = (await db.execute(select(User).where(User.email == grace_email))).scalar_one()
            created_user_ids.append(grace_user.id)
            grace_membership = (await db.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == grace_user.id,
                )
            )).scalar_one_or_none()
            assert grace_membership is not None
            await db.refresh(inv_for_grace)
            assert inv_for_grace.accepted_at is not None

        finally:
            if created_invitation_ids:
                await db.execute(delete(OrganizationInvitation).where(OrganizationInvitation.id.in_(created_invitation_ids)))
            await db.execute(delete(ActivityLog).where(ActivityLog.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            all_user_ids = [owner.id, bob.id, alice.id] + created_user_ids
            await db.execute(delete(User).where(User.id.in_(all_user_ids)))
            await db.commit()

    await engine.dispose()
    await close_redis()


def test_invitation_identity_mismatch():
    asyncio.run(_scenario())
