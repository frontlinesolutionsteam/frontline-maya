"""
payments.py — Stripe Payment Module (Maya 2.0)

Option A: Terminal  — staff taps "Charge $X" on dashboard → physical reader
Option B: Pay Link  — Checkout Session link SMSed to customer after order
Option C: Cash      — staff marks paid with cash (no Stripe)
"""

import os
import asyncio
import logging
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

logger = logging.getLogger(__name__)

_stripe_ready: bool | None = None


def _init_stripe() -> bool:
    global _stripe_ready
    if _stripe_ready is not None:
        return _stripe_ready
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if key and not key.startswith("sk_test_YOUR"):
        import stripe as _s
        _s.api_key = key
        _stripe_ready = True
    else:
        _stripe_ready = False
    return _stripe_ready


def is_stripe_enabled() -> bool:
    key = os.getenv("STRIPE_SECRET_KEY", "")
    return bool(key and not key.startswith("sk_test_YOUR"))


# ── Option A: Stripe Terminal ─────────────────────────────────────────────────

async def charge_terminal(order_id: str, amount_cents: int, reader_id: str) -> dict:
    """
    Create a PaymentIntent and present it to the Stripe Terminal reader.
    Customer taps/inserts card → Stripe fires payment_intent.succeeded webhook.
    Returns {"payment_intent_id": str, "status": "processing"}
    """
    if not _init_stripe():
        raise RuntimeError("STRIPE_SECRET_KEY not configured")

    import stripe
    loop = asyncio.get_running_loop()

    intent = await loop.run_in_executor(None, lambda: stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        payment_method_types=["card_present"],
        capture_method="automatic",
        metadata={"order_id": order_id, "source": "maya_terminal"},
    ))

    await loop.run_in_executor(None, lambda: stripe.terminal.Reader.process_payment_intent(
        reader_id,
        payment_intent=intent["id"],
    ))

    logger.info(f"Terminal charge started | order={order_id} | intent={intent['id']} | reader={reader_id}")
    return {"payment_intent_id": intent["id"], "status": "processing"}


# ── Option B: Stripe Payment Link (Checkout Session) ─────────────────────────

async def create_payment_link(
    order_id: str,
    amount_cents: int,
    items: list,
    customer_name: str,
) -> dict:
    """
    Create a one-time Stripe Checkout Session for the order total.
    Returns {"checkout_url": str, "session_id": str}
    """
    if not _init_stripe():
        raise RuntimeError("STRIPE_SECRET_KEY not configured")

    import stripe
    loop = asyncio.get_running_loop()
    base_url = os.getenv("BASE_URL", "http://localhost:8000")

    # Build a readable item description
    item_name = f"Order #{order_id[:8].upper()}"
    if items:
        names = ", ".join(f"{i.get('quantity', 1)}x {i.get('name', '')}" for i in items[:3])
        if len(items) > 3:
            names += f" +{len(items) - 3} more"
        item_name = names

    session = await loop.run_in_executor(None, lambda: stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": item_name,
                    "description": f"Pickup order for {customer_name}",
                },
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{base_url}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/payment/cancel",
        metadata={"order_id": order_id, "source": "maya_payment_link"},
        expires_at=int(time.time()) + 3600,  # 1-hour expiry
    ))

    logger.info(f"Payment link created | order={order_id} | session={session['id']}")
    return {"checkout_url": session["url"], "session_id": session["id"]}


async def send_payment_sms(
    to_phone: str,
    payment_url: str,
    amount: float,
    restaurant_name: str,
) -> bool:
    """Send payment link to customer via Twilio SMS. Returns True on success."""
    try:
        sid        = os.getenv("TWILIO_ACCOUNT_SID", "")
        token      = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_phone = os.getenv("TWILIO_PHONE_NUMBER", "")

        if not all([sid, token, from_phone]):
            logger.warning("Twilio SMS creds missing — payment SMS not sent")
            return False

        # Normalize to E.164
        to_e164 = f"+1{to_phone}" if not to_phone.startswith("+") else to_phone

        body = (
            f"Hi! Your {restaurant_name} order is ready.\n"
            f"Total: ${amount:.2f}\n"
            f"Pay here: {payment_url}\n"
            f"Link expires in 1 hour."
        )

        from twilio.rest import Client as TwilioClient
        client = TwilioClient(sid, token)
        loop   = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: client.messages.create(
            body=body,
            from_=from_phone,
            to=to_e164,
        ))
        logger.info(f"Payment SMS sent | to={to_e164}")
        return True

    except Exception as e:
        logger.error(f"Payment SMS failed: {e}")
        return False


# ── Webhook Verification ───────────────────────────────────────────────────────

def verify_payment_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify and parse a Stripe payment webhook event."""
    if not _init_stripe():
        raise RuntimeError("Stripe not configured")
    import stripe
    secret = os.getenv("STRIPE_PAYMENT_WEBHOOK_SECRET", "")
    if not secret:
        raise ValueError("STRIPE_PAYMENT_WEBHOOK_SECRET not set")
    return stripe.Webhook.construct_event(payload, sig_header, secret)
