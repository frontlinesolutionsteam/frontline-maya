"""
menu_parser.py — Menu Upload, Parsing, Validation, and Storage (Maya 2.0)
SQLite is the primary store. Airtable is an optional secondary sink.
"""

import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from execution.order_store import save_menu_db, load_menu_db, load_all_menus_db

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

logger = logging.getLogger(__name__)

# In-memory cache: { restaurant_id: config_dict }
_menu_cache: dict = {}

# Lazy Airtable client
_airtable = None

def _get_airtable():
    global _airtable
    if _airtable is None:
        from pyairtable import Api as AirtableApi
        key = os.getenv("AIRTABLE_API_KEY", "")
        if not key:
            return None
        _airtable = AirtableApi(key)
    return _airtable


# ── Parse & Validate ──────────────────────────────────────────────────────────

def parse_and_validate(raw: dict) -> tuple[dict, list]:
    """
    Validate and normalize a raw menu JSON upload.
    Returns (config, warnings). Raises ValueError on critical failures.
    """
    warnings = []

    required = ["restaurant_id", "restaurant_name", "manager_phone", "menu"]
    for field in required:
        if not raw.get(field):
            raise ValueError(f"Missing required field: '{field}'")

    if not raw.get("menu"):
        raise ValueError("Menu must contain at least one item")

    normalized_items = []
    for item in raw["menu"]:
        if not item.get("name"):
            warnings.append(f"Skipped item with no name: {item}")
            continue
        price = item.get("price")
        if not isinstance(price, (int, float)) or price < 0:
            warnings.append(f"Skipped item with invalid price: '{item.get('name')}'")
            continue

        normalized_items.append({
            "id":          item.get("id") or _slugify(item["name"]),
            "category":    item.get("category", "Other").strip(),
            "name":        item["name"].strip(),
            "description": item.get("description", "").strip(),
            "price":       round(float(price), 2),
            "available":   item.get("available", True),
            "modifiers": [
                {
                    "name":        m.get("name", "").strip(),
                    "price_delta": round(float(m.get("price_delta", 0)), 2),
                }
                for m in item.get("modifiers", []) if m.get("name")
            ],
            "combos": [
                {
                    "name":  c.get("name", "").strip(),
                    "items": c.get("items", []),
                    "price": round(float(c.get("price", 0)), 2),
                }
                for c in item.get("combos", []) if c.get("name")
            ],
        })

    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    hours_raw = raw.get("hours", {})
    normalized_hours = {}
    for day in days:
        day_h = hours_raw.get(day)
        normalized_hours[day] = (
            {"open": day_h.get("open", ""), "close": day_h.get("close", "")}
            if day_h else None
        )

    config = {
        "restaurant_id":             raw["restaurant_id"].strip(),
        "restaurant_name":           raw["restaurant_name"].strip(),
        "manager_phone":             raw["manager_phone"].strip(),
        "manager_email":             raw.get("manager_email", "").strip(),
        "twilio_phone":              raw.get("twilio_phone", "").strip(),
        "timezone":                  raw.get("timezone", "UTC").strip(),
        "hours":                     normalized_hours,
        "prep_time_estimate_minutes": int(raw.get("prep_time_estimate_minutes",
                                                   int(os.getenv("PREP_TIME_MINUTES", "10")))),
        "catering_threshold": raw.get("catering_threshold", {
            "min_dollars": float(os.getenv("CATERING_THRESHOLD_DOLLARS", "150"))
        }),
        "menu":       normalized_items,
        "updated_at": datetime.utcnow().isoformat(),
    }
    return config, warnings


# ── Save ──────────────────────────────────────────────────────────────────────

async def save_menu(config: dict) -> str:
    """Save menu to SQLite (primary) + Airtable (optional)."""
    rid = await save_menu_db(config)
    _menu_cache[rid] = config

    # Optionally mirror to Airtable
    asyncio.create_task(_save_menu_airtable(config))
    return rid


async def _save_menu_airtable(config: dict):
    at = _get_airtable()
    if not at:
        return
    base_id = os.getenv("AIRTABLE_BASE_ID", "")
    if not base_id:
        return
    try:
        table = at.table(base_id, "Menus")
        fields = {
            "restaurant_id":              config["restaurant_id"],
            "restaurant_name":            config["restaurant_name"],
            "manager_phone":              config.get("manager_phone", ""),
            "manager_email":              config.get("manager_email", ""),
            "hours_json":                 json.dumps(config["hours"]),
            "prep_time_minutes":          config["prep_time_estimate_minutes"],
            "catering_threshold_dollars": config["catering_threshold"].get("min_dollars", 150),
            "menu_json":                  json.dumps(config["menu"]),
            "updated_at":                 config["updated_at"],
        }
        loop = asyncio.get_running_loop()
        def upsert():
            existing = table.all(formula=f"{{restaurant_id}}='{config['restaurant_id']}'")
            if existing:
                table.update(existing[0]["id"], fields)
            else:
                table.create(fields)
        await loop.run_in_executor(None, upsert)
    except Exception as e:
        logger.warning(f"Airtable menu sync failed (non-fatal): {e}")


# ── Load ──────────────────────────────────────────────────────────────────────

async def load_menu(restaurant_id: str) -> dict | None:
    if restaurant_id in _menu_cache:
        return _menu_cache[restaurant_id]
    config = await load_menu_db(restaurant_id)
    if config:
        _menu_cache[restaurant_id] = config
    return config


async def load_all_menus():
    """Populate cache from SQLite at server startup."""
    configs = await load_all_menus_db()
    for c in configs:
        _menu_cache[c["restaurant_id"]] = c
    logger.info(f"Loaded {len(configs)} menu(s) into cache")
    return configs


def get_cached_menus() -> dict:
    return dict(_menu_cache)


# ── Hours Logic ───────────────────────────────────────────────────────────────

def is_restaurant_open(config: dict) -> bool:
    from datetime import datetime as dt, timezone, timedelta
    tz_name = config.get("timezone", "UTC")

    # Try pytz first (handles DST), fall back to zoneinfo, then UTC offset
    now = None
    try:
        import pytz
        tz = pytz.timezone(tz_name)
        now = dt.now(tz)
    except Exception:
        pass

    if now is None:
        try:
            from zoneinfo import ZoneInfo
            now = dt.now(ZoneInfo(tz_name))
        except Exception:
            pass

    if now is None:
        # Hard fallback: use UTC — log loudly so we know timezone lookup failed
        now = dt.now(timezone.utc)
        logger.warning(f"is_restaurant_open: timezone lookup failed for '{tz_name}', using UTC")

    day_name = now.strftime("%A").lower()
    hours = config.get("hours", {}).get(day_name)
    logger.info(
        f"is_restaurant_open | tz={tz_name} | local={now.strftime('%Y-%m-%d %H:%M %Z')} "
        f"| day={day_name} | hours={hours}"
    )
    if not hours:
        logger.info("is_restaurant_open → CLOSED (no hours for today)")
        return False
    try:
        oh, om = map(int, hours["open"].split(":"))
        ch, cm = map(int, hours["close"].split(":"))
        open_t  = now.replace(hour=oh, minute=om, second=0, microsecond=0)
        close_t = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
        result  = open_t <= now <= close_t
        logger.info(f"is_restaurant_open → {'OPEN' if result else 'CLOSED'} (open={hours['open']} close={hours['close']})")
        return result
    except Exception as e:
        logger.error(f"is_restaurant_open: comparison error: {e}")
        return True  # Default open — safer than refusing calls on bad config


def _fmt_time_spoken(hhmm: str) -> str:
    """Convert 24h 'HH:MM' to natural spoken form TTS reads correctly.
    '10:00' → '10 AM'  |  '20:00' → '8 PM'  |  '13:30' → '1:30 PM'
    """
    try:
        h, m = map(int, hhmm.split(":"))
        period = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12} {period}" if m == 0 else f"{h12}:{m:02d} {period}"
    except Exception:
        return hhmm


def get_hours_text(config: dict) -> str:
    """
    Build a natural spoken hours string, grouping consecutive days with identical hours.
    Example: 'Monday through Friday 10 AM to 8 PM, and Saturday 10 AM to 4 PM'
    """
    ordered = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    names   = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    schedule = []
    for day, name in zip(ordered, names):
        h = config.get("hours", {}).get(day)
        if h and h.get("open") and h.get("close"):
            schedule.append((name, h["open"], h["close"]))

    if not schedule:
        return "hours not available"

    # Group consecutive days that share the same open/close times
    groups = []
    i = 0
    while i < len(schedule):
        name_start, op, cl = schedule[i]
        j = i + 1
        while j < len(schedule) and schedule[j][1] == op and schedule[j][2] == cl:
            j += 1
        name_end = schedule[j - 1][0]
        span = f"{name_start} through {name_end}" if name_start != name_end else name_start
        groups.append(f"{span} {_fmt_time_spoken(op)} to {_fmt_time_spoken(cl)}")
        i = j

    if len(groups) == 1:
        return groups[0]
    return ", ".join(groups[:-1]) + ", and " + groups[-1]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    return text.lower().strip().replace(" ", "_").replace("/", "_")[:32]
