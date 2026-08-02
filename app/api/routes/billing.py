import logging

import stripe
from fastapi import APIRouter, Depends, Request
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AppException, ErrorDef
from app.core.config import settings
from app.core.database import get_db
from app.core.tenant import TenantContext, get_tenant_context, require_org_admin
from app.models.organization import Subscription
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.organization import AddonUpdateRequest, BillingPortalResponse, BillingStatusRead, SubscriptionRead
from app.services import stripe_service

logger = logging.getLogger("billing")

router = APIRouter(prefix="/billing", tags=["Billing"])

_LOCKED_STATUSES = ("past_due", "incomplete_expired", "cancelled")

_NO_SUBSCRIPTION = ErrorDef(
    code="NO_SUBSCRIPTION",
    status=http_status.HTTP_404_NOT_FOUND,
    message="This organization has no billing subscription on file.",
)
_WEBHOOK_INVALID = ErrorDef(
    code="WEBHOOK_INVALID",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="Invalid Stripe webhook payload or signature.",
)


@router.get("/status", response_model=BillingStatusRead)
async def get_billing_status(tenant: TenantContext = Depends(get_tenant_context)):
    """Any org member can check billing status (read-only) — the trial
    banner and past-due lockout need to be visible to non-admins too, even
    though only admins can act on it via /portal-session."""
    sub = tenant.organization.subscription
    if sub is None:
        return BillingStatusRead(status="active", trial_ends_at=None, is_locked=False)
    return BillingStatusRead(
        status=sub.status,
        trial_ends_at=sub.trial_ends_at,
        is_locked=sub.status in _LOCKED_STATUSES,
    )


@router.post("/portal-session", response_model=BillingPortalResponse)
async def create_portal_session(tenant: TenantContext = Depends(require_org_admin)):
    sub = tenant.organization.subscription
    if sub is None or not sub.stripe_customer_id:
        raise AppException(_NO_SUBSCRIPTION)
    session = stripe_service.create_billing_portal_session(
        customer_id=sub.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/organization?tab=billing",
    )
    return BillingPortalResponse(url=session["url"])


@router.put("/addons", response_model=SubscriptionRead)
async def update_addons(
    payload: AddonUpdateRequest,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    sub = tenant.organization.subscription
    if sub is None or not sub.stripe_subscription_id:
        raise AppException(_NO_SUBSCRIPTION)

    team_price = stripe_service.addon_price_id("extra_team", sub.billing_interval)
    user_price = stripe_service.addon_price_id("extra_user", sub.billing_interval)

    new_team_item_id = stripe_service.add_or_update_addon_item(
        sub.stripe_subscription_id, sub.stripe_extra_teams_item_id, team_price, payload.extra_teams,
    )
    new_user_item_id = stripe_service.add_or_update_addon_item(
        sub.stripe_subscription_id, sub.stripe_extra_users_item_id, user_price, payload.extra_users,
    )

    sub.extra_teams = payload.extra_teams
    sub.extra_users = payload.extra_users
    sub.stripe_extra_teams_item_id = new_team_item_id
    sub.stripe_extra_users_item_id = new_user_item_id
    await db.commit()
    await db.refresh(sub)
    return SubscriptionRead.model_validate(sub)


@router.post("/webhook", status_code=http_status.HTTP_204_NO_CONTENT)
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """No auth dependency — Stripe calls this directly. Signature verification
    via the raw body is the only trust boundary here."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe_service.construct_webhook_event(payload, sig_header)
    except (ValueError, stripe.SignatureVerificationError):
        raise AppException(_WEBHOOK_INVALID)

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type in (
        "customer.subscription.updated",
        "customer.subscription.created",
        "customer.subscription.deleted",
    ):
        await _sync_subscription(db, data, event_type)
    elif event_type == "invoice.payment_failed":
        await _set_subscription_status_from_invoice(db, data, "past_due")
    elif event_type == "invoice.paid":
        await _set_subscription_status_from_invoice(db, data, "active")
    else:
        logger.info("Unhandled Stripe webhook event type: %s", event_type)

    return None


async def _find_subscription_row(db: AsyncSession, stripe_subscription_id: str | None, org_id: str | None) -> Subscription | None:
    if stripe_subscription_id:
        result = await db.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == stripe_subscription_id)
        )
        sub = result.scalar_one_or_none()
        if sub:
            return sub
    if org_id:
        repo = OrganizationRepository(db)
        return await repo.get_subscription(org_id)
    return None


async def _sync_subscription(db: AsyncSession, stripe_sub: dict, event_type: str) -> None:
    org_id = (stripe_sub.get("metadata") or {}).get("organization_id")
    sub = await _find_subscription_row(db, stripe_sub.get("id"), org_id)
    if sub is None:
        logger.warning("Stripe webhook %s: no matching Subscription row (stripe_id=%s, org_id=%s)", event_type, stripe_sub.get("id"), org_id)
        return

    if event_type == "customer.subscription.deleted":
        sub.status = "cancelled"
        await db.commit()
        return

    fields = stripe_service.extract_subscription_fields(stripe_sub)
    sub.status = fields["status"] or sub.status
    sub.current_period_start = fields["current_period_start"] or sub.current_period_start
    sub.current_period_end = fields["current_period_end"] or sub.current_period_end
    sub.cancel_at_period_end = fields["cancel_at_period_end"]
    if fields["trial_ends_at"] is not None:
        sub.trial_ends_at = fields["trial_ends_at"]
    if not sub.stripe_subscription_id:
        sub.stripe_subscription_id = stripe_sub.get("id")
    await db.commit()


async def _set_subscription_status_from_invoice(db: AsyncSession, invoice: dict, status: str) -> None:
    stripe_subscription_id = invoice.get("subscription")
    org_id = (invoice.get("metadata") or {}).get("organization_id") if invoice.get("metadata") else None
    sub = await _find_subscription_row(db, stripe_subscription_id, org_id)
    if sub is None:
        logger.warning("Stripe invoice webhook: no matching Subscription row (stripe_subscription_id=%s)", stripe_subscription_id)
        return
    sub.status = status
    await db.commit()
