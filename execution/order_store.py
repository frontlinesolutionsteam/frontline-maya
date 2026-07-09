"""
order_store.py — SQLite Order & Menu Database
Primary local store for all orders and menus. Zero external dependencies.
Airtable is an optional secondary sink — orders are safe here regardless.
"""

import json
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# DB_PATH can be overridden via env var so Railway Volumes persist orders across deploys.
# Set DB_PATH=/data/maya.db in Railway and attach a volume at /data.
_db_path_env = os.getenv("DB_PATH", "")
DB_PATH = Path(_db_path_env) if _db_path_env else Path(__file__).resolve().parent.parent / ".tmp" / "maya.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_db_initialized = False
_init_lock = asyncio.Lock()


@asynccontextmanager
async def _get_db():
    """Async context manager that opens, configures, and closes a DB connection."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db


async def init_db():
    global _db_initialized
    async with _init_lock:
        if _db_initialized:
            return
        async with _get_db() as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id             TEXT PRIMARY KEY,
                    restaurant_id        TEXT NOT NULL,
                    timestamp            TEXT NOT NULL,
                    customer_name        TEXT,
                    customer_phone       TEXT,
                    pickup_time          TEXT,
                    order_type           TEXT DEFAULT 'standard',
                    items_json           TEXT DEFAULT '[]',
                    subtotal             REAL DEFAULT 0.0,
                    estimated_prep_minutes INTEGER DEFAULT 10,
                    special_instructions TEXT DEFAULT '',
                    status               TEXT DEFAULT 'confirmed',
                    call_sid             TEXT,
                    call_duration_seconds INTEGER DEFAULT 0,
                    payment_status          TEXT DEFAULT 'unpaid',
                    payment_method          TEXT DEFAULT '',
                    stripe_payment_intent_id TEXT DEFAULT '',
                    stripe_checkout_session_id TEXT DEFAULT '',
                    source               TEXT DEFAULT 'voice',
                    updated_at           TEXT
                );

                CREATE TABLE IF NOT EXISTS menus (
                    restaurant_id             TEXT PRIMARY KEY,
                    restaurant_name           TEXT NOT NULL,
                    manager_phone             TEXT,
                    manager_email             TEXT,
                    twilio_phone              TEXT,
                    timezone                  TEXT DEFAULT 'UTC',
                    hours_json                TEXT DEFAULT '{}',
                    prep_time_minutes         INTEGER DEFAULT 10,
                    catering_threshold_dollars REAL DEFAULT 150.0,
                    menu_json                 TEXT DEFAULT '[]',
                    updated_at                TEXT
                );

                CREATE TABLE IF NOT EXISTS billing_accounts (
                    email                    TEXT PRIMARY KEY,
                    restaurant_id            TEXT,
                    store_name               TEXT,
                    status                   TEXT DEFAULT 'trial',
                    stripe_customer_id       TEXT,
                    stripe_subscription_id   TEXT,
                    activated_at             TEXT,
                    updated_at               TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_orders_restaurant ON orders(restaurant_id);
                CREATE INDEX IF NOT EXISTS idx_orders_status     ON orders(status);
                CREATE INDEX IF NOT EXISTS idx_orders_timestamp  ON orders(timestamp);
                CREATE INDEX IF NOT EXISTS idx_menus_twilio      ON menus(twilio_phone);
            """)
            await db.commit()

            # Migrations — add payment columns to existing DBs (safe to re-run)
            for col, col_def in [
                ("payment_status",          "TEXT DEFAULT 'unpaid'"),
                ("payment_method",          "TEXT DEFAULT ''"),
                ("stripe_payment_intent_id","TEXT DEFAULT ''"),
                ("stripe_checkout_session_id","TEXT DEFAULT ''"),
                ("source",                  "TEXT DEFAULT 'voice'"),
                ("payment_reference_id",    "TEXT DEFAULT ''"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_def}")
                    await db.commit()
                    logger.info(f"DB migration: added orders.{col}")
                except Exception:
                    pass  # Column already exists

        _db_initialized = True
        logger.info(f"SQLite DB initialized at {DB_PATH}")


# ── Order Operations ──────────────────────────────────────────────────────────

async def save_order(order: dict) -> str:
    await init_db()
    customer = order.get("customer", {})
    async with _get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO orders
            (order_id, restaurant_id, timestamp, customer_name, customer_phone,
             pickup_time, order_type, items_json, subtotal, estimated_prep_minutes,
             special_instructions, status, call_sid, call_duration_seconds, source,
             payment_method, payment_status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order.get("order_id", ""),
            order.get("restaurant_id", ""),
            order.get("timestamp", datetime.utcnow().isoformat()),
            customer.get("name", ""),
            customer.get("phone", ""),
            customer.get("pickup_time", ""),
            order.get("order_type", "standard"),
            json.dumps(order.get("items", [])),
            float(order.get("subtotal", 0.0)),
            int(order.get("estimated_prep_minutes", 10)),
            order.get("special_instructions", ""),
            order.get("status", "confirmed"),
            order.get("call_sid", ""),
            int(order.get("call_duration_seconds", 0)),
            order.get("source", "voice"),
            order.get("payment_method", ""),
            order.get("payment_status", "unpaid"),
            datetime.utcnow().isoformat(),
        ))
        await db.commit()
    return order.get("order_id", "")


async def update_order_status(order_id: str, status: str) -> bool:
    await init_db()
    valid = {"confirmed", "in_prep", "ready", "picked_up", "escalated", "pending_callback", "cancelled"}
    if status not in valid:
        return False
    async with _get_db() as db:
        cursor = await db.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
            (status, datetime.utcnow().isoformat(), order_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_order_payment(
    order_id: str,
    payment_status: str,
    payment_method: str = "",
    stripe_payment_intent_id: str = "",
    stripe_checkout_session_id: str = "",
    payment_reference_id: str = "",
) -> bool:
    await init_db()
    async with _get_db() as db:
        cursor = await db.execute(
            """UPDATE orders SET
               payment_status=?, payment_method=?,
               stripe_payment_intent_id=?, stripe_checkout_session_id=?,
               payment_reference_id=?,
               updated_at=?
               WHERE order_id=?""",
            (payment_status, payment_method,
             stripe_payment_intent_id, stripe_checkout_session_id,
             payment_reference_id,
             datetime.utcnow().isoformat(), order_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_orders(restaurant_id: str = None, limit: int = 200, status: str = None) -> list:
    await init_db()
    async with _get_db() as db:
        clauses, params = [], []
        if restaurant_id:
            clauses.append("restaurant_id = ?")
            params.append(restaurant_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = await db.execute_fetchall(
            f"SELECT * FROM orders {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        )
        return [dict(row) for row in rows]


async def get_order(order_id: str) -> dict | None:
    await init_db()
    async with _get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        )
        return dict(rows[0]) if rows else None


SERVICE_FEE = 0.99


async def get_revenue_stats() -> dict:
    """Return monthly order counts and $0.99 service-fee revenue for the agency dashboard."""
    await init_db()
    async with _get_db() as db:
        current_month = datetime.utcnow().strftime("%Y-%m")

        # Billable orders in current month broken down by source
        source_rows = await db.execute_fetchall(
            """
            SELECT COALESCE(NULLIF(source,''), 'unknown') AS source, COUNT(*) AS cnt
            FROM orders
            WHERE strftime('%Y-%m', timestamp) = ?
              AND status NOT IN ('cancelled')
            GROUP BY source
            ORDER BY cnt DESC
            """,
            (current_month,),
        )
        by_source = {row["source"]: row["cnt"] for row in source_rows}

        # Current month totals
        total_rows = await db.execute_fetchall(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status NOT IN ('cancelled') THEN 1 ELSE 0 END) AS billable,
              SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
            FROM orders
            WHERE strftime('%Y-%m', timestamp) = ?
            """,
            (current_month,),
        )
        t = dict(total_rows[0]) if total_rows else {}
        billable = int(t.get("billable") or 0)
        cancelled = int(t.get("cancelled") or 0)

        # Rolling 12-month history (most recent first)
        history_rows = await db.execute_fetchall(
            """
            SELECT
              strftime('%Y-%m', timestamp) AS month,
              COUNT(*) AS total,
              SUM(CASE WHEN status NOT IN ('cancelled') THEN 1 ELSE 0 END) AS billable
            FROM orders
            GROUP BY strftime('%Y-%m', timestamp)
            ORDER BY month DESC
            LIMIT 12
            """,
        )
        history = [
            {
                "month": row["month"],
                "total": int(row["total"] or 0),
                "billable": int(row["billable"] or 0),
                "revenue": round(int(row["billable"] or 0) * SERVICE_FEE, 2),
            }
            for row in history_rows
        ]

        return {
            "current_month": current_month,
            "billable_orders": billable,
            "cancelled_orders": cancelled,
            "total_orders": billable + cancelled,
            "revenue": round(billable * SERVICE_FEE, 2),
            "service_fee": SERVICE_FEE,
            "by_source": by_source,
            "history": history,
        }


# ── Menu Operations ───────────────────────────────────────────────────────────

async def save_menu_db(config: dict) -> str:
    await init_db()
    async with _get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO menus
            (restaurant_id, restaurant_name, manager_phone, manager_email, twilio_phone,
             timezone, hours_json, prep_time_minutes, catering_threshold_dollars, menu_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            config["restaurant_id"],
            config["restaurant_name"],
            config.get("manager_phone", ""),
            config.get("manager_email", ""),
            config.get("twilio_phone", ""),
            config.get("timezone", "UTC"),
            json.dumps(config.get("hours", {})),
            int(config.get("prep_time_estimate_minutes", 10)),
            float(config.get("catering_threshold", {}).get("min_dollars", 150)),
            json.dumps(config.get("menu", [])),
            datetime.utcnow().isoformat(),
        ))
        await db.commit()
    return config["restaurant_id"]


async def load_menu_db(restaurant_id: str) -> dict | None:
    await init_db()
    async with _get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM menus WHERE restaurant_id = ?", (restaurant_id,)
        )
        if not rows:
            return None
        return _row_to_menu(dict(rows[0]))


async def load_menu_by_phone(twilio_phone: str) -> dict | None:
    """Look up restaurant config by the Twilio number that was called."""
    await init_db()
    normalized = twilio_phone.replace("+1", "").replace("-", "").replace(" ", "")
    async with _get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM menus WHERE replace(replace(replace(twilio_phone, '+1', ''), '-', ''), ' ', '') = ?",
            (normalized,),
        )
        if not rows:
            return None
        return _row_to_menu(dict(rows[0]))


async def load_all_menus_db() -> list:
    await init_db()
    async with _get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM menus")
        return [_row_to_menu(dict(row)) for row in rows]


def _row_to_menu(row: dict) -> dict:
    return {
        "restaurant_id":             row["restaurant_id"],
        "restaurant_name":           row["restaurant_name"],
        "manager_phone":             row.get("manager_phone", ""),
        "manager_email":             row.get("manager_email", ""),
        "twilio_phone":              row.get("twilio_phone", ""),
        "timezone":                  row.get("timezone", "UTC"),
        "hours":                     json.loads(row.get("hours_json", "{}")),
        "prep_time_estimate_minutes": int(row.get("prep_time_minutes", 10)),
        "catering_threshold":        {"min_dollars": float(row.get("catering_threshold_dollars", 150))},
        "menu":                      json.loads(row.get("menu_json", "[]")),
        "updated_at":                row.get("updated_at", ""),
    }


# ── Billing Account Operations ────────────────────────────────────────────────

async def save_billing_account(email: str, account: dict):
    await init_db()
    async with _get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO billing_accounts
            (email, restaurant_id, store_name, status, stripe_customer_id,
             stripe_subscription_id, activated_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            email,
            account.get("restaurant_id", ""),
            account.get("store_name", ""),
            account.get("status", "trial"),
            account.get("stripe_customer_id", ""),
            account.get("stripe_subscription_id", ""),
            account.get("activated_at", ""),
            datetime.utcnow().isoformat(),
        ))
        await db.commit()


async def get_billing_account(email: str) -> dict | None:
    await init_db()
    async with _get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM billing_accounts WHERE email = ?", (email,)
        )
        return dict(rows[0]) if rows else None


async def get_billing_account_by_restaurant(restaurant_id: str) -> dict | None:
    await init_db()
    async with _get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM billing_accounts WHERE restaurant_id = ?", (restaurant_id,)
        )
        return dict(rows[0]) if rows else None


async def get_billing_account_by_stripe_customer(customer_id: str) -> dict | None:
    await init_db()
    async with _get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM billing_accounts WHERE stripe_customer_id = ?", (customer_id,)
        )
        return dict(rows[0]) if rows else None
