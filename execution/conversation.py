"""
conversation.py — Claude Conversation Engine (Maya 2.0)
Manages per-call state, system prompt injection, and AI-driven responses.
Ref: architecture/sop_call_flow.md, sop_order.md, sop_escalation.md
"""

import os
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import anthropic

logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

_anthropic_client: Optional[anthropic.Anthropic] = None

def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment variables.")
        _anthropic_client = anthropic.Anthropic(api_key=key)
    return _anthropic_client


MANAGER_PHONE       = os.getenv("MANAGER_PHONE", "")
PREP_TIME           = int(os.getenv("PREP_TIME_MINUTES", "10"))
CATERING_THRESHOLD  = float(os.getenv("CATERING_THRESHOLD_DOLLARS", "150"))
MODEL               = "claude-haiku-4-5-20251001"   # fastest model — critical for sub-2s responses
MAX_TOKENS          = 250   # new flow is 1-2 sentences + compact JSON — 250 is plenty
MAX_HISTORY_MSGS    = 12    # simple flow = ~6 turns max; keep context tight for speed


def _extract_json(text: str) -> dict | None:
    """
    Robustly extract the first complete JSON object from text.
    Handles: leading/trailing prose, markdown fences, Claude going off-script.
    Returns None if no valid JSON object found.
    """
    # 1. Direct parse (happy path)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown code fences
    if "```" in text:
        for block in text.split("```"):
            cleaned = block.strip().lstrip("json").strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                continue

    # 3. Scan every { position until we find a parseable JSON object.
    #    Critical: if the first { leads to invalid JSON (e.g. "{restaurant}" in prose),
    #    we must continue scanning — not bail with None immediately.
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            return None
        depth, end = 0, -1
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return None  # No matching close brace anywhere
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pos = start + 1  # This { wasn't valid JSON — try the next one

# ── Per-call state store (keyed by call_sid) ─────────────────────────────────
_call_states: dict = {}


def _restaurant_now(config: dict) -> datetime:
    """Return current datetime in the restaurant's local timezone (defaults to Pacific)."""
    tz_name = config.get("timezone", "America/Los_Angeles")
    try:
        import pytz
        return datetime.now(pytz.timezone(tz_name))
    except Exception:
        pass
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        pass
    # Hard fallback: UTC-8 (PST — won't observe DST but beats UTC by 8h)
    return datetime.now(timezone(timedelta(hours=-8)))


def get_or_create_state(call_sid: str, restaurant_config: dict) -> dict:
    if call_sid not in _call_states:
        _call_states[call_sid] = {
            "call_sid":           call_sid,
            "order_id":           str(uuid.uuid4()),
            "restaurant_id":      restaurant_config.get("restaurant_id", "default"),
            "restaurant_name":    restaurant_config.get("restaurant_name", "the restaurant"),
            "stage":              "greeting",
            "items":              [],
            "subtotal":           0.0,
            "customer":           {"name": None, "phone": None, "pickup_time": None},
            "upsell_attempted":   False,
            "special_instructions": "",
            "call_start":         _restaurant_now(restaurant_config).isoformat(),
            "messages":           [],
            "escalation_reason":  None,
            "silence_count":      0,
            "detected_language":  "en",  # ISO 639-1; set on first non-English utterance
            "language_confirmed": False,  # True after one detection attempt (avoids re-detecting)
        }
    return _call_states[call_sid]


def get_state(call_sid: str) -> Optional[dict]:
    return _call_states.get(call_sid)


def clear_state(call_sid: str):
    _call_states.pop(call_sid, None)


# ── System Prompt Builder ─────────────────────────────────────────────────────
def _build_hours_block(restaurant_config: dict) -> str:
    """Build a current-time + hours block for Claude's system prompt."""
    now      = _restaurant_now(restaurant_config)
    day_name = now.strftime("%A").lower()
    hours    = restaurant_config.get("hours", {}).get(day_name)

    time_str = now.strftime("%-I:%M %p %Z")  # e.g. "9:15 PM PDT"
    day_str  = now.strftime("%A")             # e.g. "Saturday"

    if not hours:
        return (
            f"CURRENT TIME: {time_str} ({day_str})\n"
            f"HOURS TODAY: Closed (no hours configured for {day_str}).\n"
            f"⚠️ The restaurant is CLOSED right now. Do NOT take an order. "
            f"Tell the caller we're closed today and give them the manager number.\n"
        )

    try:
        oh, om = map(int, hours["open"].split(":"))
        ch, cm = map(int, hours["close"].split(":"))
        open_mins  = oh * 60 + om
        close_mins = ch * 60 + cm
        now_mins   = now.hour * 60 + now.minute

        def fmt(h, m):
            period = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            return f"{h12}:{m:02d} {period}" if m else f"{h12} {period}"

        hours_str = f"{fmt(oh, om)} – {fmt(ch, cm)}"

        if open_mins <= now_mins < close_mins:
            return (
                f"CURRENT TIME: {time_str} ({day_str})\n"
                f"HOURS TODAY: {hours_str} — WE ARE OPEN.\n"
            )
        else:
            return (
                f"CURRENT TIME: {time_str} ({day_str})\n"
                f"HOURS TODAY: {hours_str} — WE ARE CLOSED RIGHT NOW.\n"
                f"⚠️ Do NOT take an order. Tell the caller we're closed, give them today's hours ({hours_str}), "
                f"and offer the manager number for urgent needs.\n"
            )
    except Exception:
        return f"CURRENT TIME: {time_str} ({day_str})\n"


def build_system_prompt(restaurant_config: dict, state: dict) -> str:
    name          = restaurant_config.get("restaurant_name", "the restaurant")
    manager_phone = restaurant_config.get("manager_phone", MANAGER_PHONE)
    prep_time     = restaurant_config.get("prep_time_estimate_minutes", PREP_TIME)
    menu          = restaurant_config.get("menu", [])

    # Manager phone — digit by digit for TTS
    digits = manager_phone.replace("-", "").replace(" ", "").replace("+1", "")
    mgr_spoken = ", ".join(digits) if len(digits) == 10 else manager_phone

    # Flat menu list — name, price, available modifiers only (no descriptions to keep prompt short)
    menu_lines = []
    for item in menu:
        if not item.get("available", True):
            continue
        line = f"  {item['name']} — ${item['price']:.2f}"
        mods = [m["name"] for m in item.get("modifiers", []) if m.get("name")]
        if mods:
            line += f"  (options: {', '.join(mods)})"
        menu_lines.append(line)

    # Live order state — injected every turn so Claude always knows where it stands
    items_summary = ", ".join(
        f"{i.get('quantity',1)}× {i.get('name','')} (${i.get('line_total',0):.2f})"
        for i in state.get("items", [])
    ) or "nothing yet"
    subtotal  = state.get("subtotal", 0.0)
    customer  = state.get("customer", {})
    stage     = state.get("stage", "greeting")

    # Language block — keep non-English callers in their language
    lang           = state.get("detected_language", "en")
    lang_confirmed = state.get("language_confirmed", False)
    if lang != "en":
        lang_block = f"LANGUAGE: Caller speaks {lang}. Respond entirely in {lang}.\n\n"
    elif not lang_confirmed:
        lang_block = (
            'LANGUAGE: If the caller speaks Spanish or another language, add "detected_language":"<code>" '
            'to your JSON and switch to that language for the rest of the call.\n\n'
        )
    else:
        lang_block = ""

    hours_block = _build_hours_block(restaurant_config)

    return f"""You are Maya, a fast AI phone receptionist for {name}. Take takeout orders simply and quickly. This is a voice call — be brief and natural.

{hours_block}
{lang_block}MENU:
{chr(10).join(menu_lines) if menu_lines else "  (menu not loaded — take order by name)"}

ORDER SO FAR: {items_summary}  |  Total: ${subtotal:.2f}
Customer phone: already captured from caller ID — never ask for it
Stage: {stage}

━━━ 3-STEP FLOW ━━━

STEP 1 — TAKE THE ORDER
• When customer names an item, use action "add_item" and confirm it: "Got it! Anything else?"
• If the item needs a meat choice and they didn't say which meat → ask ONLY: "What meat?"
• Wait for their answer, then use "add_item" with the meat included.
• Never volunteer a list of options. Never ask about modifications, sides, or drinks unless the customer brings it up.
• Keep asking "Anything else?" until they say they're done.

STEP 2 — ONE FINAL CONFIRMATION + NAME (when customer says they're done)
• Combine the order summary and name request in a single sentence:
  "Perfect — [items], that comes to $X. What's your name?"
• Use action "readback".

STEP 3 — SUBMIT (when customer gives their name)
• Say a short goodbye: "Great, [name]! See you in about {prep_time} minutes. Goodbye!"
• Use action "submit" and include customer_field="name", customer_value="[their name]".
• This is the LAST message Maya sends. Do NOT ask any more questions after this.
• If Stage is "readback" and you couldn't make out the name, say ONLY "Sorry, what was your name?" with action="readback". Never re-list the order. Never restart.

━━━ RULES ━━━
• Max 1–2 short sentences per response. No lists. No menus unless asked.
• Never ask for phone number, pickup time, modifications, or payment.
• If customer asks what's on the menu → name 3–4 popular items, not the full list.
• Off-menu item or complaint → give manager number {mgr_spoken} and use action "escalate".
• Never fabricate prices — use menu prices above.

━━━ JSON FORMAT — return ONLY valid JSON, nothing else ━━━
{{"speech":"...","action":"continue|add_item|readback|submit|escalate_ask|escalate|end","item":{{"name":"","quantity":1,"unit_price":0.00,"line_total":0.00,"modifiers":[],"menu_item_id":""}},"customer_field":"name|null","customer_value":"value|null","detected_language":"es"}}

Action reference:
  add_item  → include item object, confirm aloud
  readback  → order summary + ask for name (Step 2)
  submit    → include customer_field="name" + customer_value; ends the call
  escalate  → transfer to manager
  continue  → everything else (clarifying questions, "what meat?", etc.)
"""


# ── Main Conversation Handler ─────────────────────────────────────────────────
def process_turn(call_sid: str, transcript: str, restaurant_config: dict) -> dict:
    """
    Process one customer utterance through Claude.
    Returns: {"speech": str, "action": str, "state": dict}
    """
    state = get_or_create_state(call_sid, restaurant_config)

    # Add user turn to history
    state["messages"].append({"role": "user", "content": transcript})

    # Trim history to prevent context overflow (keep last N messages)
    if len(state["messages"]) > MAX_HISTORY_MSGS:
        state["messages"] = state["messages"][-MAX_HISTORY_MSGS:]
    # Anthropic API requires the first message to have role "user".
    while state["messages"] and state["messages"][0]["role"] != "user":
        state["messages"].pop(0)

    system_prompt = build_system_prompt(restaurant_config, state)

    try:
        response = _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=state["messages"],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API error | model={MODEL} | call_sid={call_sid} | {type(e).__name__}: {e}")
        return {
            "speech": f"I'm having a technical issue. Please call our team directly at {MANAGER_PHONE}. So sorry!",
            "action": "escalate",
            "state": state,
            "error": str(e),
        }

    # Robust JSON extraction — handles prose/fences/going-off-script
    parsed = _extract_json(raw)
    if not parsed:
        # Claude returned something completely unparseable — give a safe fallback
        parsed = {"speech": "Sorry, I had a hiccup. Could you repeat that?", "action": "continue"}

    # Language detection: lock in the caller's language on first non-English signal.
    # Only attempt detection once (language_confirmed prevents re-checking every turn).
    if not state.get("language_confirmed"):
        detected = parsed.get("detected_language", "")
        if detected and isinstance(detected, str):
            detected = detected.lower().strip()
            # Accept only valid ISO 639-1 codes (2-3 chars); ignore anything suspicious
            if 2 <= len(detected) <= 3 and detected.isalpha() and detected != "en":
                state["detected_language"] = detected
                logger.info(f"Language detected | call_sid={call_sid} | language={detected}")
        state["language_confirmed"] = True

    # Add assistant turn to history (raw JSON keeps context tight)
    state["messages"].append({"role": "assistant", "content": raw})

    speech = parsed.get("speech", "")
    action = parsed.get("action", "continue")

    # ── Apply state mutations ─────────────────────────────────────────────────

    if action == "add_item" and parsed.get("item"):
        item = parsed["item"]
        qty  = max(1, int(item.get("quantity", 1)))
        item["quantity"] = qty

        # Always resolve price from menu — Claude's unit_price is unreliable
        item_name_lower = item.get("name", "").lower().strip()
        menu_match = next(
            (m for m in restaurant_config.get("menu", [])
             if m.get("name", "").lower().strip() == item_name_lower
             or item_name_lower in m.get("name", "").lower()
             or m.get("name", "").lower() in item_name_lower),
            None
        )
        if menu_match:
            item["unit_price"]   = menu_match["price"]
            item["menu_item_id"] = menu_match.get("id", item.get("menu_item_id", ""))
        elif not item.get("unit_price"):
            item["unit_price"] = 0.00

        item["line_total"] = round(float(item["unit_price"]) * qty, 2)
        state["items"].append(item)
        state["subtotal"] = round(sum(i["line_total"] for i in state["items"]), 2)

        # If the customer modifies during readback, reset to ordering so a new
        # readback is required — the auto-submit guard must not fire mid-edit.
        if state.get("stage") == "readback":
            state["stage"] = "ordering"

        if state["subtotal"] > CATERING_THRESHOLD and state["stage"] != "catering":
            state["stage"] = "catering"
            action = "catering"

    # Mark upsell as attempted once the call advances past the ordering phase.
    # Claude's action schema has no "upsell" action — tracking by stage transition
    # is the only reliable signal that the upsell window has closed.
    if action in ("collect_info", "readback", "submit") and state.get("items"):
        state["upsell_attempted"] = True

    if action in ("collect_info", "readback", "submit", "catering", "escalate_ask", "escalate"):
        state["stage"] = action

    if action == "collect_info":
        field = parsed.get("customer_field")
        value = parsed.get("customer_value")

        # If Claude failed to extract the name (customer_value is null) but the caller
        # clearly said something, pull the name straight from the raw transcript.
        # This prevents the "asked for name twice" loop caused by garbled STT output.
        if field == "name" and not value and transcript.strip():
            cleaned = transcript.strip()
            # Strip common verbal prefixes callers use before their name
            for prefix in ("my name is ", "i'm ", "i am ", "this is ", "it's ", "its "):
                if cleaned.lower().startswith(prefix):
                    cleaned = cleaned[len(prefix):].strip()
                    break
            # Only accept as a name if it's concise (1–5 words) — avoids swallowing
            # full sentences that happen to arrive when stage is collect_info.
            if cleaned and len(cleaned.split()) <= 5:
                value = cleaned.title()
                logger.info(f"Name extracted from transcript fallback | call_sid={call_sid} | name='{value}'")

        if field in state["customer"] and value:
            state["customer"][field] = value

    # Also accept customer_field inside a "submit" action (allows phone + close in one turn)
    if action == "submit" and parsed.get("customer_field") and parsed.get("customer_value"):
        field = parsed["customer_field"]
        value = parsed["customer_value"]
        if field in state["customer"] and value:
            state["customer"][field] = value

    # Pickup time is NEVER asked — auto-set it the moment we have name + phone.
    if state["customer"].get("name") and state["customer"].get("phone"):
        if not state["customer"].get("pickup_time"):
            state["customer"]["pickup_time"] = f"~{PREP_TIME} minutes"

    # Safety guard: if Claude tries to submit but pickup_time is still missing, set it now.
    if action == "submit" and not state["customer"].get("pickup_time"):
        state["customer"]["pickup_time"] = f"~{PREP_TIME} minutes"

    # ── AUTO-SUBMIT GUARD ─────────────────────────────────────────────────────
    # Safety net: only fires AFTER the customer has already heard the readback
    # (stage == "readback") and Claude forgot to emit "submit" on confirmation.
    # Do NOT fire earlier — phone is always pre-set from caller ID, so firing on
    # name+phone+items would skip the entire modification/name/readback flow.
    # "add_item" and "collect_info" are excluded so customers can still modify
    # during readback — the stage resets to "ordering" when add_item fires.
    _ready_to_submit = (
        state["customer"].get("name") and
        state["customer"].get("phone") and
        state.get("items") and
        state.get("stage") == "readback" and
        action not in ("submit", "escalate", "escalate_ask", "end", "catering",
                       "escalate", "add_item", "collect_info")
    )
    if _ready_to_submit:
        action = "submit"
        state["stage"] = "submit"

    if action == "escalate":
        state["escalation_reason"] = parsed.get("escalation_reason", "off_scope")
        state["stage"] = "escalated"

    # Hard guard: never allow "end" while items are in the cart and unsubmitted.
    # If Claude tries to end prematurely, steer toward submit (or collect name first).
    if action == "end" and state.get("items"):
        if not state["customer"].get("name"):
            action = "continue"
        else:
            action = "submit"
            state["stage"] = "submit"

    if action == "end":
        state["stage"] = "ended"

    return {"speech": speech, "action": action, "state": state}


# ── Static Phrases ────────────────────────────────────────────────────────────

# Translations for the closed-hours and timeout messages.
# Greeting stays English — language detection requires the caller to speak first.
_CLOSED_TEMPLATES: dict[str, str] = {
    "en": "Welcome to {r}! We're currently closed. For urgent matters, you can reach our manager at {p}. We're open {h}. We look forward to seeing you soon!",
    "es": "¡Bienvenido a {r}! Actualmente estamos cerrados. Para asuntos urgentes, puede comunicarse con nuestro gerente al {p}. Estamos abiertos {h}. ¡Esperamos verle pronto!",
    "fr": "Bienvenue chez {r}! Nous sommes actuellement fermés. Pour les affaires urgentes, contactez notre gérant au {p}. Nous sommes ouverts {h}. À bientôt!",
    "de": "Willkommen bei {r}! Wir sind derzeit geschlossen. Für dringende Anliegen erreichen Sie unseren Manager unter {p}. Geöffnet: {h}. Wir freuen uns auf Ihren Besuch!",
    "pt": "Bem-vindo ao {r}! Estamos fechados no momento. Para assuntos urgentes, fale com nosso gerente no {p}. Estamos abertos {h}. Até logo!",
    "zh": "欢迎光临{r}！我们目前已打烊。如有紧急事项，请致电{p}联系我们的经理。营业时间：{h}。期待您的光临！",
    "ja": "{r}へようこそ！現在は閉店しております。緊急の場合は{p}にてマネージャーにご連絡ください。営業時間：{h}。またのご来店をお待ちしております。",
    "ko": "{r}에 오신 것을 환영합니다! 현재 영업 종료 상태입니다. 긴급한 경우 {p}로 매니저에게 연락해 주세요. 영업 시간: {h}. 곧 뵙겠습니다!",
    "ar": "مرحباً بكم في {r}! نحن مغلقون حالياً. للأمور العاجلة، يمكنكم التواصل مع مديرنا على {p}. أوقات العمل: {h}. نتطلع لرؤيتكم قريباً!",
    "hi": "{r} में आपका स्वागत है! हम अभी बंद हैं। जरूरी मामलों के लिए {p} पर हमारे मैनेजर से संपर्क करें। हम {h} खुले हैं। जल्द मिलते हैं!",
}

_TIMEOUT_TEMPLATES: dict[str, str] = {
    "en": "I want to make sure you get the best help. You can reach our manager directly at {p} — again, that's {p}. Thanks so much for calling {r}, have a wonderful day!",
    "es": "Quiero asegurarme de que reciba la mejor ayuda. Puede comunicarse con nuestro gerente directamente al {p} — de nuevo, ese número es {p}. ¡Muchas gracias por llamar a {r}, que tenga un excelente día!",
    "fr": "Je veux m'assurer que vous recevez la meilleure aide. Vous pouvez joindre notre gérant directement au {p} — encore une fois, c'est {p}. Merci beaucoup d'avoir appelé {r}, bonne journée!",
    "de": "Ich möchte sicherstellen, dass Sie die beste Hilfe bekommen. Sie können unseren Manager direkt unter {p} erreichen — nochmals, das ist {p}. Vielen Dank für Ihren Anruf bei {r}, schönen Tag!",
    "pt": "Quero garantir que você receba a melhor ajuda. Você pode falar com nosso gerente diretamente no {p} — de novo, é {p}. Muito obrigado por ligar para {r}, tenha um ótimo dia!",
    "zh": "我想确保您得到最好的帮助。您可以直接拨打{p}联系我们的经理——再一次，号码是{p}。非常感谢您致电{r}，祝您有美好的一天！",
    "ja": "最善のサポートをご提供できるよう、{p}にてマネージャーに直接ご連絡ください。もう一度、{p}です。{r}にお電話いただきありがとうございました。良い一日をお過ごしください！",
    "ko": "최상의 도움을 드리고 싶습니다. {p}로 매니저에게 직접 연락하실 수 있습니다. 다시 한번, {p}입니다. {r}에 전화해 주셔서 감사합니다. 좋은 하루 되세요!",
    "ar": "أريد التأكد من حصولكم على أفضل مساعدة. يمكنكم التواصل مع مديرنا مباشرةً على {p} — مرة أخرى، الرقم هو {p}. شكراً جزيلاً لاتصالكم بـ{r}، أتمنى لكم يوماً رائعاً!",
    "hi": "मैं चाहता हूं कि आपको सर्वोत्तम सहायता मिले। आप हमारे मैनेजर से सीधे {p} पर संपर्क कर सकते हैं — फिर से, वह {p} है। {r} पर कॉल करने के लिए धन्यवाद, आपका दिन शुभ हो!",
}


def generate_greeting(restaurant_name: str, language: str = "en") -> str:
    # Greeting stays in English — language detection requires the caller to speak first.
    return f"This is {restaurant_name}, I'm Maya the AI ordering assistant — what can I get you?"


def generate_closed_message(restaurant_name: str, hours_text: str, manager_phone: str, language: str = "en") -> str:
    spoken_phone = _format_phone_spoken(manager_phone)
    template = _CLOSED_TEMPLATES.get(language, _CLOSED_TEMPLATES["en"])
    return template.format(r=restaurant_name, h=hours_text, p=spoken_phone)


def generate_timeout_response(restaurant_name: str, manager_phone: str, language: str = "en") -> str:
    spoken = _format_phone_spoken(manager_phone)
    template = _TIMEOUT_TEMPLATES.get(language, _TIMEOUT_TEMPLATES["en"])
    return template.format(r=restaurant_name, p=spoken)


def _format_phone_spoken(number: str) -> str:
    n = number.replace("-", "").replace(" ", "").replace("+1", "")
    if len(n) == 10:
        return f"{n[0]}, {n[1]}, {n[2]}, {n[3]}, {n[4]}, {n[5]}, {n[6]}, {n[7]}, {n[8]}, {n[9]}"
    return number
