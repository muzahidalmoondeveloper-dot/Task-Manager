import logging
from datetime import datetime, timezone
from typing import Any

import stripe

from app.core.config import settings
from app.models.organization import Organization
from app.models.user import User

logger = logging.getLogger("stripe_service")

stripe.api_key = settings.STRIPE_SECRET_KEY

_BASE_PRICE_TABLE = {
    ("starter", "monthly"): lambda: settings.STRIPE_PRICE_STARTER_MONTHLY,
    ("starter", "annual"): lambda: settings.STRIPE_PRICE_STARTER_ANNUAL,
    ("business", "monthly"): lambda: settings.STRIPE_PRICE_BUSINESS_MONTHLY,
    ("business", "annual"): lambda: settings.STRIPE_PRICE_BUSINESS_ANNUAL,
}

_ADDON_PRICE_TABLE = {
    ("extra_team", "monthly"): lambda: settings.STRIPE_PRICE_EXTRA_TEAM_MONTHLY,
    ("extra_team", "annual"): lambda: settings.STRIPE_PRICE_EXTRA_TEAM_ANNUAL,
    ("extra_user", "monthly"): lambda: settings.STRIPE_PRICE_EXTRA_USER_MONTHLY,
    ("extra_user", "annual"): lambda: settings.STRIPE_PRICE_EXTRA_USER_ANNUAL,
}


def base_price_id(plan: str, interval: str) -> str:
    getter = _BASE_PRICE_TABLE.get((plan, interval))
    price_id = getter() if getter else None
    if not price_id:
        raise ValueError(f"No Stripe price configured for plan={plan!r} interval={interval!r}")
    return price_id


def addon_price_id(addon: str, interval: str) -> str:
    getter = _ADDON_PRICE_TABLE.get((addon, interval))
    price_id = getter() if getter else None
    if not price_id:
        raise ValueError(f"No Stripe price configured for addon={addon!r} interval={interval!r}")
    return price_id


def create_customer(org: Organization, owner_user: User) -> "stripe.Customer":
    return stripe.Customer.create(
        email=owner_user.email,
        name=org.name,
        metadata={"organization_id": str(org.id)},
    )


def create_trial_subscription(
    customer_id: str, plan: str, interval: str, org_id: str, trial_days: int | None = None,
) -> "stripe.Subscription":
    """Creates the org's subscription with a no-card-required trial. Stripe
    will attempt to invoice automatically when the trial ends; if there's no
    payment method on file that invoice fails and the subscription flips to
    past_due — that transition arrives via the `invoice.payment_failed` /
    `customer.subscription.updated` webhooks, not synchronously here."""
    return stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": base_price_id(plan, interval), "quantity": 1}],
        trial_period_days=trial_days or settings.STRIPE_TRIAL_DAYS,
        trial_settings={"end_behavior": {"missing_payment_method": "create_invoice"}},
        payment_behavior="default_incomplete",
        metadata={"organization_id": str(org_id)},
        expand=["latest_invoice.payment_intent"],
    )


def _find_item(subscription: "stripe.Subscription", price_id: str) -> "stripe.SubscriptionItem | None":
    for item in subscription["items"]["data"]:
        if item["price"]["id"] == price_id:
            return item
    return None


def add_or_update_addon_item(
    subscription_id: str,
    existing_item_id: str | None,
    price_id: str,
    quantity: int,
) -> str | None:
    """Create/update/remove a single add-on line item on an existing
    subscription. Returns the new item ID (or None if removed)."""
    if quantity <= 0:
        if existing_item_id:
            stripe.SubscriptionItem.delete(existing_item_id)
        return None

    if existing_item_id:
        item = stripe.SubscriptionItem.modify(existing_item_id, quantity=quantity)
        return item["id"]

    item = stripe.SubscriptionItem.create(
        subscription=subscription_id, price=price_id, quantity=quantity,
    )
    return item["id"]


def create_billing_portal_session(customer_id: str, return_url: str) -> "stripe.billing_portal.Session":
    return stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)


def cancel_subscription(subscription_id: str, at_period_end: bool = True) -> "stripe.Subscription":
    if at_period_end:
        return stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)
    return stripe.Subscription.delete(subscription_id)


def construct_webhook_event(payload: bytes, sig_header: str) -> "stripe.Event":
    return stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)


def _to_datetime(unix_ts: int | None) -> datetime | None:
    if unix_ts is None:
        return None
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)


def extract_subscription_fields(subscription: dict[str, Any]) -> dict[str, Any]:
    """Normalizes the bits of a Stripe Subscription object this app persists,
    shared by the webhook handler for every event that carries a subscription."""
    return {
        "status": subscription.get("status"),
        "current_period_start": _to_datetime(subscription.get("current_period_start")),
        "current_period_end": _to_datetime(subscription.get("current_period_end")),
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
        "trial_ends_at": _to_datetime(subscription.get("trial_end")),
    }
