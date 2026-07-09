"""
square_payments.py — Square Payment Integration Seam (Maya 2.0)

Square isn't wired up yet, so process_square_payment() mocks a successful
charge. This is the only function callers touch — swap its body for real
Square Web Payments SDK (client-side tokenization) + Orders/Payments API
calls (server-side) later without changing main.py or the dashboard.
"""

import asyncio
import logging
import os
import uuid

logger = logging.getLogger(__name__)


def is_square_configured() -> bool:
    """Real Square wiring would check SQUARE_ACCESS_TOKEN / SQUARE_LOCATION_ID here."""
    return bool(os.getenv("SQUARE_ACCESS_TOKEN") and os.getenv("SQUARE_LOCATION_ID"))


async def process_square_payment(order: dict) -> dict:
    """
    STUB — mocks a successful Square charge for `order`.

    Real implementation:
      1. Client tokenizes the card with the Square Web Payments SDK, sending
         a nonce/source_id to this endpoint instead of just an order_id.
      2. Server calls Square's CreatePayment (Payments API) with that
         source_id, amount_money, and SQUARE_LOCATION_ID.
      3. Return the real payment id/status instead of the mocked ones below.
    """
    amount = float(order.get("subtotal", 0.0))
    await asyncio.sleep(0.4)  # simulate the network round-trip to Square
    payment_id = f"sq_mock_{uuid.uuid4().hex[:12]}"
    logger.info(
        f"[Square STUB] Mock charge succeeded | order={order.get('order_id')} | "
        f"amount=${amount:.2f} | payment_id={payment_id}"
    )
    return {
        "status": "success",
        "square_payment_id": payment_id,
        "amount": amount,
    }
