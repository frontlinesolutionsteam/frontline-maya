"""
delivery.py — Order Delivery Engine (Maya 2.0)
Writes to SQLite first (always), then fans out to Airtable, Dashboard, Email, Slack.
All external sinks are optional — orders are never lost even if every API is down.
All clients are lazy-initialized.
"""

import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

import httpx
from execution.order_store import save_order

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

logger = logging.getLogger(__name__)
TMP_DIR = Path(__file__).resolve().parent.parent / ".tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# Connected dashboard WebSocket clients (populated by main.py)
dashboard_clients: set = set()

# Lazy clients
_airtable  = None
_slack     = None

def _get_airtable():
    global _airtable
    if _airtable is None:
        key = os.getenv("AIRTABLE_API_KEY", "")
        if not key:
            return None
        from pyairtable import Api as AirtableApi
        _airtable = AirtableApi(key)
    return _airtable

def _get_slack():
    global _slack
    if _slack is None:
        token = os.getenv("SLACK_BOT_TOKEN", "")
        if not token:
            return None
        from slack_sdk import WebClient as SlackClient
        _slack = SlackClient(token=token)
    return _slack


# ── Main Delivery ─────────────────────────────────────────────────────────────

async def deliver_order(order: dict) -> dict:
    """
    Write order to SQLite first (durable), then fan out to all sinks simultaneously.
    Returns per-sink result report.
    """
    # SQLite write is synchronous and primary — must succeed
    try:
        await save_order(order)
        sqlite_result = "OK"
    except Exception as e:
        logger.error(f"SQLite order write FAILED | order_id={order.get('order_id')} | {e}")
        sqlite_result = f"FAILED: {e}"

    # Fan out to all optional external sinks simultaneously
    results = await asyncio.gather(
        _write_airtable(order),
        _broadcast_dashboard(order),
        _send_email(order),
        _post_slack(order),
        return_exceptions=True,
    )

    targets = ["airtable", "dashboard", "email", "slack"]
    report  = {"sqlite": sqlite_result}
    for target, result in zip(targets, results):
        if isinstance(result, Exception):
            logger.warning(f"Delivery sink failed [{target}]: {result}")
            report[target] = f"FAILED: {result}"
        else:
            report[target] = result or "OK"

    logger.info(f"Order delivered | order_id={order.get('order_id')} | {report}")
    return report


# ── 1. Airtable ───────────────────────────────────────────────────────────────

async def _write_airtable(order: dict, retries: int = 2) -> str:
    at = _get_airtable()
    if not at:
        return "SKIPPED (not configured)"
    base_id = os.getenv("AIRTABLE_BASE_ID", "")
    if not base_id:
        return "SKIPPED (AIRTABLE_BASE_ID not set)"

    customer = order.get("customer", {})
    fields = {
        "order_id":               order.get("order_id", ""),
        "restaurant_id":          order.get("restaurant_id", ""),
        "timestamp":              order.get("timestamp", datetime.utcnow().isoformat()),
        "customer_name":          customer.get("name", ""),
        "customer_phone":         customer.get("phone", ""),
        "pickup_time":            customer.get("pickup_time", ""),
        "order_type":             order.get("order_type", "standard"),
        "items_json":             json.dumps(order.get("items", [])),
        "subtotal":               float(order.get("subtotal", 0)),
        "estimated_prep_minutes": int(order.get("estimated_prep_minutes", 10)),
        "special_instructions":   order.get("special_instructions", ""),
        "status":                 order.get("status", "confirmed"),
        "call_sid":               order.get("call_sid", ""),
        "call_duration_seconds":  int(order.get("call_duration_seconds", 0)),
    }
    table = at.table(base_id, "Orders")
    for attempt in range(retries):
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: table.create(fields))
            return "OK"
        except Exception as e:
            if attempt == retries - 1:
                raise e
            await asyncio.sleep(2 ** attempt)
    return "OK"


# ── 2. Dashboard WebSocket Broadcast ─────────────────────────────────────────

async def _broadcast_dashboard(order: dict) -> str:
    if not dashboard_clients:
        return "OK (no clients)"
    message = json.dumps({"event": "new_order", "order": order})
    dead = set()
    for ws in list(dashboard_clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    dashboard_clients.difference_update(dead)  # in-place; avoids Python UnboundLocalError
    return "OK"


async def broadcast_payment_update(order_id: str, payment_status: str, payment_method: str = ""):
    """Notify dashboard clients of a payment status change."""
    if not dashboard_clients:
        return
    message = json.dumps({
        "event": "payment_update",
        "order_id": order_id,
        "payment_status": payment_status,
        "payment_method": payment_method,
    })
    dead = set()
    for ws in list(dashboard_clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    dashboard_clients.difference_update(dead)


async def broadcast_status_update(order_id: str, status: str):
    """Notify dashboard clients of a status change."""
    if not dashboard_clients:
        return
    message = json.dumps({"event": "status_update", "order_id": order_id, "status": status})
    dead = set()
    for ws in list(dashboard_clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    dashboard_clients.difference_update(dead)  # in-place; avoids Python UnboundLocalError


# ── 3. SendGrid Email ─────────────────────────────────────────────────────────

async def _send_email(order: dict, retries: int = 2) -> str:
    sendgrid_key = os.getenv("SENDGRID_API_KEY", "")
    order_email  = os.getenv("ORDER_NOTIFICATION_EMAIL", "")
    from_email   = os.getenv("SENDGRID_FROM_EMAIL", order_email)

    if not sendgrid_key or not order_email:
        return "SKIPPED (SendGrid not configured)"

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    customer   = order.get("customer", {})
    items      = order.get("items", [])
    order_type = order.get("order_type", "standard")

    items_rows = ""
    for item in items:
        mods    = ", ".join(item.get("modifiers", []))
        mod_str = f" ({mods})" if mods else ""
        items_rows += (
            f"<tr>"
            f"<td style='padding:4px 8px'>{item.get('quantity',1)}x</td>"
            f"<td style='padding:4px 8px'><strong>{item.get('name','')}</strong>{mod_str}</td>"
            f"<td style='padding:4px 8px;text-align:right'>${item.get('line_total',0):.2f}</td>"
            f"</tr>"
        )

    subject = (
        f"🎉 CATERING LEAD — {customer.get('name','Unknown')} | {customer.get('pickup_time','TBD')}"
        if order_type == "catering"
        else f"🍽️ New Order — {customer.get('name','Unknown')} | Pickup: {customer.get('pickup_time','ASAP')}"
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
      <div style="background:#1a1a2e;color:white;padding:20px;border-radius:8px 8px 0 0">
        <h2 style="margin:0">{'🎉 Catering Lead' if order_type == 'catering' else '🍽️ New Order — Confirmed'}</h2>
        <p style="margin:4px 0;opacity:.8">Order ID: {order.get('order_id','')}</p>
      </div>
      <div style="background:#f9f9f9;padding:20px;border:1px solid #ddd">
        <h3 style="color:#333">Customer</h3>
        <p><strong>Name:</strong> {customer.get('name','—')}</p>
        <p><strong>Phone:</strong> {customer.get('phone','—')}</p>
        <p><strong>Pickup:</strong> {customer.get('pickup_time','—')}</p>
        {'<p><strong>Headcount:</strong> ' + str(customer.get('headcount','—')) + '</p>' if order_type == 'catering' else ''}
      </div>
      {'<div style="background:white;padding:20px;border:1px solid #ddd;border-top:none"><h3 style="color:#333">Items</h3><table style="width:100%;border-collapse:collapse">' + items_rows + '</table><hr><p style="text-align:right;font-size:1.1em"><strong>Subtotal: $' + f"{order.get('subtotal',0):.2f}" + '</strong></p></div>' if items else ''}
      <div style="background:#f0f0f0;padding:15px;border-radius:0 0 8px 8px;font-size:.85em;color:#666">
        <p>Prep time: {order.get('estimated_prep_minutes',10)} min</p>
        {'<p>Special instructions: ' + order.get('special_instructions','') + '</p>' if order.get('special_instructions') else ''}
        <p>Call SID: {order.get('call_sid','—')} | Duration: {order.get('call_duration_seconds',0)}s</p>
        <p>Timestamp: {order.get('timestamp','')}</p>
      </div>
    </div>"""

    for attempt in range(retries):
        try:
            sg  = SendGridAPIClient(sendgrid_key)
            msg = Mail(from_email=from_email, to_emails=order_email,
                       subject=subject, html_content=html)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: sg.send(msg))
            return "OK"
        except Exception as e:
            if attempt == retries - 1:
                fail_log = TMP_DIR / "email_failures.log"
                with open(fail_log, "a") as f:
                    f.write(f"{datetime.utcnow().isoformat()} | order_id={order.get('order_id')} | {e}\n")
                raise e
            await asyncio.sleep(5)
    return "OK"


# ── 4. Slack ──────────────────────────────────────────────────────────────────

async def _post_slack(order: dict) -> str:
    slack = _get_slack()
    if not slack:
        return "SKIPPED (not configured)"
    channel = os.getenv("SLACK_ORDERS_CHANNEL_ID", "")
    if not channel:
        return "SKIPPED (SLACK_ORDERS_CHANNEL_ID not set)"

    customer   = order.get("customer", {})
    items      = order.get("items", [])
    order_type = order.get("order_type", "standard")
    subtotal   = order.get("subtotal", 0)

    if order_type == "catering":
        text = (
            f"🎉 *CATERING LEAD* | {customer.get('name','Unknown')} | {customer.get('pickup_time','TBD')}\n"
            f"Party of {customer.get('headcount','?')} | Call back: {customer.get('phone','—')}\n"
            f"Notes: {customer.get('notes','None')}"
        )
    else:
        items_lines = "\n".join(
            f"  {i.get('quantity',1)}x {i.get('name','')}"
            + (f" ({', '.join(i.get('modifiers',[]))})" if i.get("modifiers") else "")
            + f" — ${i.get('line_total',0):.2f}"
            for i in items
        )
        text = (
            f"🍽️ *New Order* | {customer.get('name','Unknown')} | Pickup: {customer.get('pickup_time','ASAP')}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"{items_lines}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"*Subtotal:* ${subtotal:.2f}  |  *Phone:* {customer.get('phone','—')}\n"
            f"*Order ID:* {order.get('order_id','—')}"
        )

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: slack.chat_postMessage(channel=channel, text=text, mrkdwn=True),
        )
        return "OK"
    except Exception as e:
        fail_log = TMP_DIR / "slack_failures.log"
        with open(fail_log, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} | order_id={order.get('order_id')} | {e}\n")
        raise e
