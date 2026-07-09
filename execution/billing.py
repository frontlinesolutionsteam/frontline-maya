"""
billing.py — Stripe Subscription Billing (Maya 2.0)
$500/month per restaurant. SQLite-backed account store.
All Stripe calls are lazy-initialized — missing keys never crash startup.
"""

import os
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from execution.order_store import (
    save_billing_account,
    get_billing_account,
    get_billing_account_by_restaurant,
    get_billing_account_by_stripe_customer,
)

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

_stripe_initialized = False

def _init_stripe():
    global _stripe_initialized
    if _stripe_initialized:
        return
    import stripe as _stripe
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if key and not key.startswith("sk_test_YOUR"):
        _stripe.api_key = key
        _stripe_initialized = True

def _is_stripe_enabled() -> bool:
    key = os.getenv("STRIPE_SECRET_KEY", "")
    return bool(key and not key.startswith("sk_test_YOUR"))


# ── Checkout Session ──────────────────────────────────────────────────────────

def create_checkout_session(store_name: str, owner_email: str, restaurant_id: str) -> str:
    if not _is_stripe_enabled():
        raise RuntimeError("Stripe is not configured. Set STRIPE_SECRET_KEY in environment variables.")
    _init_stripe()
    import stripe
    price_id = os.getenv("STRIPE_PRICE_ID", "")
    if not price_id:
        raise RuntimeError("STRIPE_PRICE_ID not set in environment variables.")

    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        customer_email=owner_email,
        line_items=[{"price": price_id, "quantity": 1}],
        metadata={
            "store_name":    store_name,
            "owner_email":   owner_email,
            "restaurant_id": restaurant_id,
        },
        success_url=f"{BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/billing/cancel",
        subscription_data={
            "metadata": {"restaurant_id": restaurant_id, "store_name": store_name},
            "trial_period_days": 30,
        },
    )
    return session.url


# ── Webhook Handler ───────────────────────────────────────────────────────────

async def handle_webhook(payload: bytes, sig_header: str) -> dict:
    if not _is_stripe_enabled():
        return {"action": "noop", "reason": "stripe_disabled"}
    _init_stripe()
    import stripe
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET not set")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Stripe webhook signature invalid: {e}")
        raise ValueError("Invalid Stripe webhook signature")

    event_type = event["type"]
    logger.info(f"Stripe event: {event_type}")

    if event_type in ("customer.subscription.created", "invoice.payment_succeeded"):
        sub = event["data"]["object"]
        if event_type == "invoice.payment_succeeded":
            sub = stripe.Subscription.retrieve(sub.get("subscription", ""))
        meta          = sub.get("metadata", {})
        restaurant_id = meta.get("restaurant_id", "")
        store_name    = meta.get("store_name", "")
        customer_id   = sub.get("customer", "")
        email         = await _find_email(customer_id) or meta.get("owner_email", customer_id)

        account = {
            "restaurant_id":          restaurant_id,
            "store_name":             store_name,
            "status":                 "active",
            "stripe_customer_id":     customer_id,
            "stripe_subscription_id": sub.get("id", ""),
            "activated_at":           datetime.utcnow().isoformat(),
        }
        await save_billing_account(email, account)
        logger.info(f"Store activated | email={email} | restaurant_id={restaurant_id}")
        return {"action": "activated", "email": email, "restaurant_id": restaurant_id}

    elif event_type == "invoice.payment_failed":
        customer_id = event["data"]["object"].get("customer", "")
        email       = await _find_email(customer_id) or customer_id
        existing    = await get_billing_account(email) or {}
        existing["status"] = "past_due"
        await save_billing_account(email, existing)
        logger.warning(f"Payment failed | customer={customer_id}")
        return {"action": "past_due", "email": email}

    elif event_type == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer", "")
        email       = await _find_email(customer_id) or customer_id
        existing    = await get_billing_account(email) or {}
        existing["status"] = "cancelled"
        await save_billing_account(email, existing)
        logger.info(f"Subscription cancelled | customer={customer_id}")
        return {"action": "cancelled", "email": email}

    return {"action": "noop", "event": event_type}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _find_email(customer_id: str) -> str | None:
    acc = await get_billing_account_by_stripe_customer(customer_id)
    return acc["email"] if acc else None


async def is_store_active(restaurant_id: str) -> bool:
    acc = await get_billing_account_by_restaurant(restaurant_id)
    return acc is not None and acc.get("status") == "active"


STRIPE_ENABLED = _is_stripe_enabled()
