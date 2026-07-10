"""
main.py — Maya 2.0 Voice Receptionist
FastAPI server: Twilio call webhooks, ElevenLabs/Polly voice, order dashboard, billing.

Key improvements over v1:
- SQLite-first storage (Airtable is optional)
- Lazy init on every external client — missing keys never crash startup
- Twilio webhook signature validation
- Multi-restaurant routing by Twilio number called
- Order status lifecycle (confirmed → in_prep → ready → picked_up)
- ElevenLabs in call path (env flag) with Polly fallback
- /health endpoint checks all integrations live
"""

import os
import json
import asyncio
import logging
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)


def _pacific_now() -> datetime:
    """Return current datetime in US/Pacific time (handles DST automatically)."""
    try:
        import pytz
        return datetime.now(pytz.timezone("America/Los_Angeles"))
    except Exception:
        pass
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        pass
    return datetime.now(timezone(timedelta(hours=-8)))  # PST fallback


# ── Sentry error monitoring (no-op if SENTRY_DSN not set) ────────────────────
_sentry_dsn = os.getenv("SENTRY_DSN", "")
if _sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        traces_sample_rate=0.05,
        send_default_pii=False,
    )
    logging.getLogger(__name__).info("[sentry] Error monitoring active")

# ── Local modules ─────────────────────────────────────────────────────────────
from execution.conversation import (
    process_turn, generate_greeting, generate_closed_message,
    generate_timeout_response, get_state, clear_state, get_or_create_state,
)
from execution.audio_pipeline import (
    try_synthesize, transcribe_audio, prewarm_audio_cache, TMP_DIR as AUDIO_TMP,
    get_polly_voice, get_twilio_language,
)
from execution.delivery import deliver_order, dashboard_clients, broadcast_status_update, broadcast_payment_update
from execution.menu_parser import (
    parse_and_validate, save_menu, load_menu, load_all_menus, get_cached_menus,
    is_restaurant_open, get_hours_text,
)
from execution.order_store import (
    init_db, get_orders, get_order, update_order_status, update_order_payment,
)
from execution.payments import (
    charge_terminal, create_payment_link, send_payment_sms,
    verify_payment_webhook, is_stripe_enabled,
)
from execution.square_payments import process_square_payment
from execution.billing import create_checkout_session, handle_webhook, is_store_active, STRIPE_ENABLED

# ── Config ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MANAGER_PHONE = os.getenv("MANAGER_PHONE", "")
BASE_URL      = os.getenv("BASE_URL", "http://localhost:8000")
RESTAURANT_ID = os.getenv("RESTAURANT_ID", "default")
PREP_TIME     = int(os.getenv("PREP_TIME_MINUTES", "10"))
VALIDATE_TWILIO_SIG = os.getenv("VALIDATE_TWILIO_SIG", "true").lower() == "true"
DASHBOARD_PASSWORD  = os.getenv("DASHBOARD_PASSWORD", "")

# ── Lazy Twilio client ────────────────────────────────────────────────────────
_twilio_client       = None
_twilio_validator    = None

def _get_twilio():
    global _twilio_client
    if _twilio_client is None:
        from twilio.rest import Client as TwilioClient
        sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        if not sid or not token:
            raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set.")
        _twilio_client = TwilioClient(sid, token)
    return _twilio_client

def _get_validator():
    global _twilio_validator
    if _twilio_validator is None:
        from twilio.request_validator import RequestValidator
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        if not token:
            return None
        _twilio_validator = RequestValidator(token)
    return _twilio_validator


# ── Slack alerting ────────────────────────────────────────────────────────────
SLACK_TOKEN   = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_ALERT_CHANNEL_ID") or os.getenv("SLACK_ORDERS_CHANNEL_ID", "")

async def alert_slack(title: str, detail: str, level: str = "error"):
    if not SLACK_TOKEN or not SLACK_CHANNEL:
        return
    emoji = "🔴" if level == "error" else "⚠️" if level == "warning" else "ℹ️"
    text  = f"{emoji} *Maya 2.0 — {title}*\n```{detail[:1500]}```"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                json={"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True},
            )
    except Exception:
        pass


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Maya 2.0 Voice Receptionist", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

AUDIO_TMP.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=str(AUDIO_TMP)), name="audio")

# Call tracking
_call_timers: dict = {}
_call_start:  dict = {}

# ── Localized static phrases used outside of Claude's generated speech ────────
# Keys mirror the scenarios in _handle_speech_turn where we hardcode strings.
# Only languages most likely for this restaurant's market are fully translated;
# all others fall back to English (ElevenLabs still pronounces correctly).
_PHRASES: dict[str, dict[str, str]] = {
    "silence_1": {
        "en": "Take your time — what can I get started for you?",
        "es": "Tómese su tiempo — ¿qué le puedo ordenar?",
        "fr": "Prenez votre temps — que puis-je vous commander?",
        "de": "Nehmen Sie sich Zeit — was darf ich Ihnen bestellen?",
        "pt": "Tome seu tempo — o que posso pedir para você?",
        "zh": "请慢慢来——我能为您点些什么？",
        "ja": "ごゆっくりどうぞ — ご注文はいかがでしょうか？",
        "ko": "천천히 하세요 — 무엇을 주문하시겠어요?",
    },
    "silence_2": {
        "en": "I'm still here! Go ahead and tell me what you'd like to order.",
        "es": "¡Sigo aquí! Dígame qué desea pedir.",
        "fr": "Je suis toujours là ! Dites-moi ce que vous souhaitez commander.",
        "de": "Ich bin noch da! Sagen Sie mir bitte, was Sie bestellen möchten.",
        "pt": "Ainda estou aqui! Me diga o que gostaria de pedir.",
        "zh": "我还在——请告诉我您想点什么。",
        "ja": "まだおります — ご注文をお聞かせください。",
        "ko": "아직 여기 있어요! 주문을 말씀해 주세요.",
    },
    "silence_final": {
        "en": "I don't want to keep you — feel free to call back anytime! Have a great day.",
        "es": "No quiero quitarle más tiempo — ¡llámenos cuando quiera! Que tenga un buen día.",
        "fr": "Je ne veux pas vous retenir — n'hésitez pas à rappeler ! Bonne journée.",
        "de": "Ich möchte Sie nicht aufhalten — rufen Sie jederzeit zurück! Einen schönen Tag.",
        "pt": "Não quero te prender — pode ligar de volta quando quiser! Tenha um ótimo dia.",
        "zh": "不打扰您了——随时可以再打电话！祝您有美好的一天。",
        "ja": "お時間を取らせて申し訳ありません。またいつでもお電話ください！良い一日を。",
        "ko": "더 이상 귀찮게 하지 않겠습니다 — 언제든지 다시 전화하세요! 좋은 하루 보내세요.",
    },
    "needs_name": {
        "en": "Before I let you go — can I get your name for the order?",
        "es": "Antes de que se vaya — ¿me puede dar su nombre para el pedido?",
        "fr": "Avant de raccrocher — puis-je avoir votre nom pour la commande?",
        "de": "Bevor ich Sie gehen lasse — wie ist Ihr Name für die Bestellung?",
        "pt": "Antes de deixar você ir — pode me dar seu nome para o pedido?",
        "zh": "在您离开之前——能告诉我您的姓名用于订单吗？",
        "ja": "失礼ですが、ご注文のお名前をいただけますか？",
        "ko": "가시기 전에 — 주문을 위해 이름을 알려주시겠어요?",
    },
    "what_to_order": {
        "en": "I'd love to help! What would you like to order today?",
        "es": "¡Con gusto le ayudo! ¿Qué desea ordenar hoy?",
        "fr": "Je serais ravi de vous aider ! Que souhaitez-vous commander aujourd'hui?",
        "de": "Ich helfe Ihnen gerne! Was möchten Sie heute bestellen?",
        "pt": "Com prazer! O que você gostaria de pedir hoje?",
        "zh": "我很乐意帮助您！今天想点什么？",
        "ja": "喜んでお手伝いします！本日は何をご注文されますか？",
        "ko": "도와드리겠습니다! 오늘 무엇을 주문하시겠어요?",
    },
    "missing_fields": {
        "en": "Just one more thing — I still need your {fields}.",
        "es": "Solo una cosa más — aún necesito su {fields}.",
        "fr": "Encore une chose — j'ai encore besoin de votre {fields}.",
        "de": "Noch eine Sache — ich brauche noch Ihren {fields}.",
        "pt": "Só mais uma coisa — ainda preciso do seu {fields}.",
        "zh": "还有一件事——我还需要您的{fields}。",
        "ja": "もう一点だけ — まだ{fields}が必要です。",
        "ko": "한 가지만 더 — {fields}가 필요합니다.",
    },
    "silence_order_nudge": {
        "en": "I'm still here and your order is saved! Take your time — just let me know when you're ready to continue.",
        "es": "¡Sigo aquí y su pedido está guardado! Tómese su tiempo — solo dígame cuando esté listo para continuar.",
        "fr": "Je suis toujours là et votre commande est sauvegardée ! Prenez votre temps — dites-moi quand vous êtes prêt à continuer.",
        "de": "Ich bin noch da und Ihre Bestellung ist gespeichert! Nehmen Sie sich Zeit — sagen Sie mir Bescheid, wenn Sie weitermachen möchten.",
        "pt": "Ainda estou aqui e seu pedido está salvo! Sem pressa — me avise quando quiser continuar.",
        "zh": "我还在，您的订单已保存！慢慢来——准备好了告诉我继续。",
        "ja": "まだおります。ご注文は保存されています！ゆっくりどうぞ — 続ける準備ができたらお知らせください。",
        "ko": "아직 여기 있고 주문은 저장되어 있어요! 천천히 하세요 — 계속할 준비가 되면 말씀해 주세요.",
    },
    "ordering_repeat": {
        "en": "Sorry, I didn't quite catch that — could you say it again?",
        "es": "Lo siento, no lo escuché bien — ¿podría repetirlo?",
        "fr": "Désolé, je n'ai pas bien entendu — pourriez-vous répéter?",
        "de": "Entschuldigung, ich habe das nicht verstanden — könnten Sie es wiederholen?",
        "pt": "Desculpe, não entendi bem — pode repetir?",
        "zh": "抱歉，我没听清楚——能再说一遍吗？",
        "ja": "すみません、よく聞こえませんでした。もう一度おっしゃっていただけますか？",
        "ko": "죄송합니다, 잘 못 들었어요 — 다시 한 번 말씀해 주시겠어요?",
    },
    "readback_name_repeat": {
        "en": "Sorry, I didn't catch your name — what was it?",
        "es": "Lo siento, no escuché su nombre — ¿cómo se llama?",
        "fr": "Désolé, je n'ai pas saisi votre nom — comment vous appelez-vous?",
        "de": "Entschuldigung, ich habe Ihren Namen nicht verstanden — wie heißen Sie?",
        "pt": "Desculpe, não entendi seu nome — qual é o seu nome?",
        "zh": "抱歉，我没听清您的名字——您叫什么名字？",
        "ja": "すみません、お名前が聞き取れませんでした — お名前をもう一度お願いします。",
        "ko": "죄송합니다, 이름을 못 들었어요 — 성함이 어떻게 되세요?",
    },
    "tech_error": {
        "en": "I'm having a technical issue. Please call our team directly at {phone}. So sorry!",
        "es": "Tengo un problema técnico. Por favor llame a nuestro equipo directamente al {phone}. ¡Lo siento mucho!",
        "fr": "J'ai un problème technique. Veuillez appeler notre équipe directement au {phone}. Toutes mes excuses!",
        "de": "Ich habe ein technisches Problem. Bitte rufen Sie unser Team direkt an: {phone}. Es tut mir sehr leid!",
        "pt": "Estou com um problema técnico. Por favor, ligue para nossa equipe diretamente no {phone}. Muito sorry!",
        "zh": "我遇到技术问题。请直接拨打{phone}联系我们的团队。非常抱歉！",
        "ja": "技術的な問題が発生しました。{phone}まで直接お電話ください。大変申し訳ございません！",
        "ko": "기술적인 문제가 있습니다. {phone}으로 직접 팀에 연락해 주세요. 정말 죄송합니다!",
    },
}


def _phrase(key: str, language: str, **kwargs) -> str:
    """Return a localized phrase, falling back to English if the language isn't translated."""
    texts = _PHRASES.get(key, {})
    text = texts.get(language) or texts.get("en", "")
    return text.format(**kwargs) if kwargs else text


def _say(resp, text: str, language: str = "en"):
    """Add a Polly <Say> using the correct voice for the caller's detected language."""
    voice, lang_code = get_polly_voice(language)
    resp.say(text, voice=voice, language=lang_code)


# ── Gather helper ─────────────────────────────────────────────────────────────
def _make_gather(resp, *, needs_phone: bool = False, timeout: int = 15, language: str = "en"):
    """
    Build a <Gather> tag.
    speech_timeout="auto" — Twilio smart end-of-speech detection; fires ~300ms after last word
    instead of waiting a fixed silence period. Target: <1.5s from end of speech to Maya's reply.
    phone_call model is faster and lower-latency than experimental_conversations.
    Twilio auto-falls-back to phone_call if the locale is unsupported.
    """
    kwargs = dict(
        input="speech",
        action=f"{BASE_URL}/twilio/gather",
        method="POST",
        speech_timeout="auto",
        speech_model="phone_call",
        language=get_twilio_language(language),
        timeout=timeout,
    )
    if needs_phone:
        kwargs["hints"] = "zero,one,two,three,four,five,six,seven,eight,nine"
    return resp.gather(**kwargs)


def _make_record(resp, *, timeout: int = 4):
    """
    Build a <Record> that sends audio to /twilio/recording for Deepgram STT.
    timeout = seconds of silence after last word before recording ends.
    """
    resp.record(
        action=f"{BASE_URL}/twilio/recording",
        method="POST",
        timeout=timeout,
        max_length=60,
        trim="trim-silence",
        play_beep=False,
    )


async def _download_twilio_recording(recording_url: str) -> bytes:
    """
    Download a Twilio recording WAV using Basic Auth (AccountSid:AuthToken).
    Twilio recording URLs require credentials — they are NOT publicly accessible.
    """
    sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    # Twilio omits the extension; requesting .wav gives PCM audio Deepgram prefers
    url = recording_url.rstrip("/") + ".wav"
    async with httpx.AsyncClient(timeout=15, auth=(sid, token)) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


# ── Startup ───────────────────────────────────────────────────────────────────
async def _seed_menus_from_disk():
    """
    Auto-seed restaurant menus from /menus/*.json on every startup.
    Always upserts so the repo JSON is always the source of truth —
    timezone, hours, and pricing changes take effect on next deploy.
    """
    from execution.menu_parser import parse_and_validate, save_menu
    menus_dir = Path(__file__).resolve().parent / "menus"
    if not menus_dir.exists():
        return
    for path in sorted(menus_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
            rid = raw.get("restaurant_id", "")
            config, warnings = parse_and_validate(raw)
            await save_menu(config)
            logger.info(f"Menu seeded from disk: {rid} ({len(config['menu'])} items)"
                        + (f" warnings={warnings}" if warnings else ""))
        except Exception as e:
            logger.error(f"Menu seed failed for {path.name}: {e}")


@app.on_event("startup")
async def startup():
    logger.info("Maya 2.0 starting up...")
    await init_db()
    await _seed_menus_from_disk()   # restore menus after every redeploy
    await load_all_menus()
    asyncio.create_task(prewarm_audio_cache("the restaurant"))

    # Log env-var status so Railway logs immediately show what's configured/missing
    anthropic     = "✅" if os.getenv("ANTHROPIC_API_KEY")               else "❌ MISSING"
    slack_token   = "✅" if os.getenv("SLACK_BOT_TOKEN")                 else "❌ MISSING"
    slack_channel = "✅" if os.getenv("SLACK_ORDERS_CHANNEL_ID")         else "❌ MISSING"
    sendgrid      = "✅" if os.getenv("SENDGRID_API_KEY")                else "❌ MISSING"
    order_email   = "✅" if os.getenv("ORDER_NOTIFICATION_EMAIL")        else "❌ MISSING"
    elevenlabs    = "✅" if os.getenv("ELEVENLABS_API_KEY")              else "❌ MISSING"
    deepgram      = "✅" if os.getenv("DEEPGRAM_API_KEY")               else "❌ MISSING"
    stripe_key    = "✅" if is_stripe_enabled()                          else "⚠️  not set"
    stripe_reader = "✅" if os.getenv("STRIPE_TERMINAL_READER_ID")      else "⚠️  no reader yet"
    stripe_twilio = "✅" if os.getenv("TWILIO_PHONE_NUMBER")            else "⚠️  needed for SMS"
    logger.info(
        f"Env check | ANTHROPIC={anthropic} | SLACK_BOT_TOKEN={slack_token} | SLACK_ORDERS_CHANNEL_ID={slack_channel} | "
        f"SENDGRID={sendgrid} | ORDER_EMAIL={order_email} | "
        f"ELEVENLABS={elevenlabs} | DEEPGRAM={deepgram}"
    )
    logger.info(
        f"Payment env | STRIPE={stripe_key} | READER={stripe_reader} | TWILIO_PHONE={stripe_twilio}"
    )
    logger.info("Maya 2.0 is ready.")
    asyncio.create_task(alert_slack("Server Started ✅", f"Maya 2.0 live at {BASE_URL}", level="info"))


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    checks = {}

    # Claude API
    try:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        checks["claude"] = "ok" if key else "missing ANTHROPIC_API_KEY"
    except Exception as e:
        checks["claude"] = f"error: {e}"

    # Twilio
    try:
        sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        checks["twilio"] = "ok" if sid and token else "missing credentials"
    except Exception as e:
        checks["twilio"] = f"error: {e}"

    # ElevenLabs
    try:
        el_key = os.getenv("ELEVENLABS_API_KEY", "")
        use_el = os.getenv("USE_ELEVENLABS", "true").lower() == "true"
        if not use_el:
            checks["elevenlabs"] = "disabled"
        elif el_key:
            checks["elevenlabs"] = "ok"
        else:
            checks["elevenlabs"] = "missing ELEVENLABS_API_KEY"
    except Exception as e:
        checks["elevenlabs"] = f"error: {e}"

    # SQLite
    try:
        from execution.order_store import DB_PATH
        checks["sqlite"] = "ok" if DB_PATH.exists() else "db not yet created"
    except Exception as e:
        checks["sqlite"] = f"error: {e}"

    # Airtable
    airtable_key = os.getenv("AIRTABLE_API_KEY", "")
    checks["airtable"] = "ok" if airtable_key else "not configured (optional)"

    # Stripe
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    checks["stripe"] = "ok" if (stripe_key and not stripe_key.startswith("sk_test_YOUR")) else "not configured (optional)"

    # Slack
    checks["slack"] = "ok" if SLACK_TOKEN else "not configured (optional)"

    # Menus loaded
    menus = get_cached_menus()
    checks["menus_loaded"] = len(menus)

    all_ok = all(v == "ok" or v == "disabled" or str(v).startswith("not configured") or isinstance(v, int)
                 for v in checks.values())

    return {"status": "ok" if all_ok else "degraded", "checks": checks, "version": "2.0.0"}


# ══════════════════════════════════════════════════════════════════════════════
# TWILIO WEBHOOKS
# ══════════════════════════════════════════════════════════════════════════════

def _validate_twilio(request: Request, form: dict) -> bool:
    """Validate that the request came from Twilio. Skip in dev mode."""
    if not VALIDATE_TWILIO_SIG:
        return True
    validator = _get_validator()
    if not validator:
        return True
    # Railway terminates TLS at its proxy so request.url has http://.
    # Twilio signs with the public https:// URL — reconstruct it.
    url = str(request.url)
    if request.headers.get("X-Forwarded-Proto", "") == "https":
        url = "https://" + url.split("://", 1)[1]
    signature = request.headers.get("X-Twilio-Signature", "")
    return validator.validate(url, dict(form), signature)


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """
    Step 1 — Twilio calls this when a call arrives.
    Route to the right restaurant, check hours, play Maya's greeting.
    """
    form     = await request.form()
    call_sid = form.get("CallSid", "unknown")
    caller   = form.get("From", "unknown")
    to_phone = form.get("To", "")

    if not _validate_twilio(request, dict(form)):
        logger.warning(f"Invalid Twilio signature | CallSid={call_sid} | From={caller}")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    logger.info(f"Inbound call | CallSid={call_sid} | From={caller} | To={to_phone}")

    # Multi-restaurant: look up by Twilio number → restaurant config
    config = await _resolve_restaurant(to_phone)
    restaurant_name = config.get("restaurant_name", "the restaurant")
    manager_phone   = config.get("manager_phone", MANAGER_PHONE)

    # Pre-populate caller's phone from Twilio From field — no need to ask
    state = get_or_create_state(call_sid, config)
    if caller and caller not in ("unknown", ""):
        digits = caller.replace("+1", "").replace("+", "").replace("-", "").replace(" ", "")
        if len(digits) >= 10:
            state["customer"]["phone"] = digits[-10:]  # keep last 10 digits

    _call_start[call_sid] = asyncio.get_running_loop().time()

    # ── Closed hours response ─────────────────────────────────────────────────
    if not is_restaurant_open(config):
        hours_text = get_hours_text(config)
        msg   = generate_closed_message(restaurant_name, hours_text, manager_phone)
        resp  = VoiceResponse()
        audio = await try_synthesize(msg)
        if audio:
            resp.play(audio)
        else:
            _say(resp, msg)   # English — language not yet detected
        return _twiml(str(resp))

    # ── Greeting → Gather (Twilio STT inline, no separate download needed) ───────
    # <Gather> with speech_timeout="1" fires our webhook 1s after the caller stops
    # speaking — no WAV download, no Deepgram round-trip — keeps latency under 2s.
    greeting = generate_greeting(restaurant_name)
    resp     = VoiceResponse()
    audio    = await try_synthesize(greeting)
    if audio:
        resp.play(audio)
    else:
        _say(resp, greeting)   # English — language detection happens on first caller utterance
    _make_gather(resp, timeout=10)   # 10s to start speaking after greeting; "en" default is fine here

    # 4-minute call timer
    _call_timers[call_sid] = asyncio.create_task(_call_timeout_task(call_sid, config))

    return _twiml(str(resp))


async def _handle_speech_turn(call_sid: str, transcript: str, config: dict) -> str:
    """
    Core: transcript → Claude → TwiML.
    Shared by /twilio/recording (Deepgram path) and /twilio/gather (Twilio STT fallback).
    Returns a TwiML XML string.
    """
    manager_phone = config.get("manager_phone", MANAGER_PHONE)
    resp          = VoiceResponse()

    # Read caller language from state (updated after each Claude turn).
    # On silence turns we use the pre-existing detected language.
    pre_state = get_state(call_sid)
    lang      = pre_state.get("detected_language", "en") if pre_state else "en"

    # No speech — give chances before hanging up, but NEVER hang up mid-order
    if not transcript:
        silence_count = pre_state.get("silence_count", 0) if pre_state else 0
        if pre_state:
            pre_state["silence_count"] = silence_count + 1
        has_active_order = bool(pre_state and pre_state.get("items"))
        stage = pre_state.get("stage", "greeting") if pre_state else "greeting"

        # Readback stage: we already read the order, just need the name — re-ask it directly
        # without calling Claude (which might restart the order flow).
        if stage == "readback":
            if pre_state:
                pre_state["silence_count"] = 0  # reset — this is STT glitch, not true silence
            msg = _phrase("readback_name_repeat", lang)
            audio = await try_synthesize(msg)
            if audio: resp.play(audio)
            else: _say(resp, msg, lang)
            _make_gather(resp, language=lang)
            return str(resp)

        if silence_count >= 3:
            if has_active_order:
                # Order in progress — never cut the call; reset counter and nudge
                if pre_state:
                    pre_state["silence_count"] = 0
                msg = _phrase("silence_order_nudge", lang)
                audio = await try_synthesize(msg)
                if audio: resp.play(audio)
                else: _say(resp, msg, lang)
                _make_gather(resp, timeout=20, language=lang)
                return str(resp)
            msg = _phrase("silence_final", lang)
            audio = await try_synthesize(msg)
            if audio: resp.play(audio)
            else: _say(resp, msg, lang)
            _cleanup_call(call_sid)
            resp.hangup()
            return str(resp)

        # Mid-order silence: use a "repeat" prompt so the customer knows Maya is still there
        # and waiting for the same answer — not starting over.
        if has_active_order:
            msg = _phrase("ordering_repeat", lang)
        else:
            msg = _phrase("silence_1" if silence_count == 0 else "silence_2", lang)
        audio = await try_synthesize(msg)
        if audio: resp.play(audio)
        else: _say(resp, msg, lang)
        _make_gather(resp, timeout=15, language=lang)
        return str(resp)

    # ── Process with Claude (blocking sync → thread executor) ────────────────
    try:
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, process_turn, call_sid, transcript, config
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"process_turn crashed | {call_sid} | {e}\n{tb}")
        asyncio.create_task(alert_slack(
            f"process_turn crashed (CallSid {call_sid})",
            f"Transcript: {transcript}\n\nError: {e}\n\n{tb}"
        ))
        msg = _phrase("tech_error", lang, phone=_format_phone(manager_phone))
        _say(resp, msg, lang)
        _cleanup_call(call_sid)
        resp.hangup()
        return str(resp)

    speech = result["speech"]
    action = result["action"]
    state  = result["state"]

    # Use the post-turn language — Claude may have just detected it on this turn.
    lang = state.get("detected_language", "en")

    # Phone still needed? Use speech_timeout="2" for digit-by-digit pauses.
    needs_phone = not state.get("customer", {}).get("phone")

    # ── Terminal actions ──────────────────────────────────────────────────────

    if action == "end":
        # Caller just wanted info / no items — okay to say goodbye and end
        if not state.get("items"):
            audio = await try_synthesize(speech)
            if audio: resp.play(audio)
            else: _say(resp, speech, lang)
            _cleanup_call(call_sid)
            resp.hangup()
            return str(resp)
        # Items in cart but no name yet — must collect it before ending
        if not state.get("customer", {}).get("name"):
            nudge = _phrase("needs_name", lang)
            audio = await try_synthesize(nudge)
            if audio: resp.play(audio)
            else: _say(resp, nudge, lang)
            _make_gather(resp, language=lang)
            return str(resp)
        # Items + name exist — NEVER hang up without submitting; redirect to submit flow
        action = "submit"
        # falls through to the submit block below

    if action == "escalate_ask":
        audio = await try_synthesize(speech)
        if audio: resp.play(audio)
        else: _say(resp, speech, lang)
        _make_gather(resp, language=lang)
        return str(resp)

    if action == "escalate":
        audio = await try_synthesize(speech)
        if audio: resp.play(audio)
        else: _say(resp, speech, lang)
        if state.get("items"):
            asyncio.create_task(_submit_order(state, config, "escalated"))
        else:
            clear_state(call_sid)
        _cleanup_call(call_sid)
        resp.dial(_to_e164(manager_phone))
        return str(resp)

    if action == "submit":
        customer = state.get("customer", {})

        # Must have at least one item before submitting
        if not state.get("items"):
            nudge = _phrase("what_to_order", lang)
            audio = await try_synthesize(nudge)
            if audio: resp.play(audio)
            else: _say(resp, nudge, lang)
            _make_gather(resp, language=lang)
            return str(resp)

        # pickup_time is auto-set in conversation.py — only check name + phone
        missing = [f for f in ["name", "phone"] if not customer.get(f)]
        if missing:
            nudge = _phrase("missing_fields", lang, fields=" and ".join(missing))
            audio = await try_synthesize(nudge)
            if audio: resp.play(audio)
            else: _say(resp, nudge, lang)
            _make_gather(resp, needs_phone=needs_phone, language=lang)
            return str(resp)

        logger.info(f"Submitting order | CallSid={call_sid} | items={len(state.get('items',[]))} | subtotal=${state.get('subtotal',0):.2f} | customer={state.get('customer',{})} | language={lang}")

        # Always use a reliable closing — don't trust Claude's speech here.
        # Claude may write just "Perfect!" when the auto-submit guard fires mid collect_info.
        # Claude already generated closing in the target language; use it directly if available,
        # otherwise fall back to the English template (ElevenLabs will still pronounce correctly).
        name     = customer.get("name", "")
        subtotal = state.get("subtotal", 0.0)
        prep     = config.get("prep_time_estimate_minutes", PREP_TIME)
        items    = state.get("items", [])
        item_summary = ", ".join(
            f"{i.get('quantity',1)} {i.get('name','')}" for i in items
        )
        closing = speech or f"Perfect, {name}! See you in about {prep} minutes!"
        audio = await try_synthesize(closing)
        if audio: resp.play(audio)
        else: _say(resp, closing, lang)

        # Submit order as a background task — the call hangs up immediately after
        # the closing plays; we don't wait for Airtable/email/Slack delivery.
        def _on_submit_done(t):
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(f"_submit_order FAILED | CallSid={call_sid} | {exc}", exc_info=exc)
                asyncio.create_task(alert_slack(
                    f"Order submit FAILED (CallSid {call_sid})",
                    f"customer={state.get('customer')} items={len(state.get('items',[]))}\n{exc}",
                ))

        task = asyncio.create_task(_submit_order(state, config, "confirmed"))
        task.add_done_callback(_on_submit_done)
        _cleanup_call(call_sid)
        logger.info(f"Hanging up | CallSid={call_sid} | order submitted to background task")
        resp.hangup()
        return str(resp)

    # ── Continue conversation ─────────────────────────────────────────────────
    audio = await try_synthesize(speech)
    if audio: resp.play(audio)
    else: _say(resp, speech, lang)
    _make_gather(resp, needs_phone=needs_phone, language=lang)
    return str(resp)


@app.post("/twilio/recording")
async def twilio_recording(request: Request):
    """
    Primary STT path — Twilio sends raw audio here after <Record>.
    Downloads the WAV → Deepgram nova-3 → Claude → ElevenLabs/Polly.

    Deepgram nova-3 is dramatically better than Twilio's built-in STT for:
    - Heavy accents
    - Mumbling / low-energy speech
    - Spoken phone numbers (numerals=true converts them automatically)
    """
    try:
        form     = await request.form()
        call_sid = form.get("CallSid", "unknown")
        rec_url  = form.get("RecordingUrl", "")
        rec_dur  = int(form.get("RecordingDuration", "0") or "0")

        logger.info(f"Recording | CallSid={call_sid} | duration={rec_dur}s | url={rec_url}")

        config = await _resolve_restaurant_from_call(call_sid)
        transcript = ""

        if rec_url and rec_dur > 0:
            try:
                audio_bytes = await _download_twilio_recording(rec_url)
                transcript  = await transcribe_audio(audio_bytes, content_type="audio/wav")
                logger.info(f"Deepgram | CallSid={call_sid} | '{transcript}'")
            except Exception as e:
                logger.error(f"Deepgram transcription failed | {call_sid} | {e}")
                # Fall through with empty transcript — caller hears "didn't catch that"

        twiml = await _handle_speech_turn(call_sid, transcript, config)
        return _twiml(twiml)

    except Exception as e:
        # CRITICAL: never return HTTP 500 — Twilio silently hangs up on 500.
        # Always return valid TwiML so the caller hears something.
        tb = traceback.format_exc()
        logger.error(f"CRITICAL /twilio/recording crash | {e}\n{tb}")
        asyncio.create_task(alert_slack(
            "⚠️ /twilio/recording CRASH",
            f"Error: {e}\n\n{tb[:1000]}"
        ))
        resp = VoiceResponse()
        manager = os.getenv("MANAGER_PHONE", MANAGER_PHONE)
        _say(resp, f"I'm really sorry, I hit a technical issue. Please call us directly at {_format_phone(manager)}.")
        resp.hangup()
        return _twiml(str(resp))


@app.post("/twilio/gather")
async def twilio_gather(request: Request):
    """
    Primary speech handler — Twilio fires this 1s after the caller stops speaking.
    Receives Twilio's inline STT transcript, runs Claude, returns TwiML.
    No WAV download or Deepgram round-trip → keeps total latency under 2s.
    """
    try:
        form       = await request.form()
        call_sid   = form.get("CallSid", "unknown")
        transcript = form.get("SpeechResult", "").strip()
        confidence = float(form.get("Confidence", "0") or "0")
        caller     = form.get("From", "")

        logger.info(f"Gather | CallSid={call_sid} | conf={confidence:.2f} | '{transcript}'")

        # Drop only truly garbage transcripts (static, line noise, accidental triggers).
        # Threshold of 0.10 is intentionally low — proper names, accented speech, and
        # food items in Spanish routinely score 0.15–0.30 on Twilio's STT. Dropping
        # them silently causes the "asked for name twice" failure mode.
        if transcript and confidence < 0.10:
            logger.info(f"Noise-level confidence ({confidence:.2f}) dropped | {call_sid}")
            transcript = ""
        config = await _resolve_restaurant_from_call(call_sid)

        # Re-apply caller ID on every gather turn — survives server restarts mid-call
        if caller and caller not in ("unknown", ""):
            state = get_or_create_state(call_sid, config)
            if not state["customer"].get("phone"):
                digits = caller.replace("+1","").replace("+","").replace("-","").replace(" ","")
                if len(digits) >= 10:
                    state["customer"]["phone"] = digits[-10:]

        twiml  = await _handle_speech_turn(call_sid, transcript, config)
        return _twiml(twiml)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"CRITICAL /twilio/gather crash | {e}\n{tb}")
        asyncio.create_task(alert_slack("⚠️ /twilio/gather CRASH", f"{e}\n\n{tb[:1000]}"))
        resp = VoiceResponse()
        manager = os.getenv("MANAGER_PHONE", MANAGER_PHONE)
        _say(resp, f"I'm really sorry, I hit a technical issue. Please call us directly at {_format_phone(manager)}.")
        resp.hangup()
        return _twiml(str(resp))


# ══════════════════════════════════════════════════════════════════════════════
# MENU API
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/menu/upload")
async def upload_menu(file: UploadFile = File(...), key: str = ""):
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    content = await file.read()
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    try:
        config, warnings = parse_and_validate(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    restaurant_id = await save_menu(config)
    return {
        "status":          "ok",
        "restaurant_id":   restaurant_id,
        "restaurant_name": config["restaurant_name"],
        "menu_items":      len(config["menu"]),
        "warnings":        warnings,
    }


@app.get("/menu/{restaurant_id}")
async def get_menu(restaurant_id: str):
    config = await load_menu(restaurant_id)
    if not config:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    return config


@app.get("/menus")
async def list_menus():
    menus = get_cached_menus()
    return {
        "menus": [
            {
                "restaurant_id":   c["restaurant_id"],
                "restaurant_name": c["restaurant_name"],
                "twilio_phone":    c.get("twilio_phone", ""),
                "manager_phone":   c.get("manager_phone", ""),
                "menu_item_count": len(c.get("menu", [])),
                "updated_at":      c.get("updated_at", ""),
            }
            for c in menus.values()
        ]
    }


# ══════════════════════════════════════════════════════════════════════════════
# ORDERS API
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/orders")
async def api_get_orders(restaurant_id: str = None, status: str = None, limit: int = 200):
    """Fetch orders from SQLite. Optional filters: restaurant_id, status."""
    orders = await get_orders(restaurant_id=restaurant_id, status=status, limit=limit)
    return {"orders": orders}


@app.patch("/api/orders/{order_id}/status")
async def api_update_status(order_id: str, request: Request):
    """Update order status. Body: {"status": "in_prep"|"ready"|"picked_up"|...}"""
    body   = await request.json()
    status = body.get("status", "").strip()
    valid  = {"confirmed", "in_prep", "ready", "picked_up", "escalated", "pending_callback", "cancelled"}
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid status. Valid: {sorted(valid)}")
    ok = await update_order_status(order_id, status)
    if not ok:
        raise HTTPException(status_code=404, detail="Order not found")
    asyncio.create_task(broadcast_status_update(order_id, status))
    return {"status": "ok", "order_id": order_id, "new_status": status}


@app.post("/api/orders/website")
async def api_ingest_website_order(request: Request):
    """
    Receive orders from the Taqueria El Coral website (taqueria-el-coral-production.up.railway.app).
    The website POSTs its own payload format from the browser — no auth key needed.
    Normalizes to Maya's internal order schema and delivers to dashboard + Slack + email.
    """
    body = await request.json()

    raw_items = body.get("items", [])
    items = []
    for i in raw_items:
        mods = []
        # Legacy website format
        if i.get("meatChoice"):
            mods.append(i["meatChoice"])
        for excl in i.get("excludedIngredients", []):
            mods.append(f"No {excl}")
        # New direct order form format — modifiers is a list of strings
        for m in i.get("modifiers", []):
            if isinstance(m, str) and m not in mods:
                mods.append(m)
        if i.get("specialNote") and i["specialNote"] not in mods:
            mods.append(i["specialNote"])
        unit_price = float(i.get("price", 0))
        qty        = int(i.get("quantity", 1))
        items.append({
            "name":       i.get("name", ""),
            "quantity":   qty,
            "modifiers":  mods,
            "unit_price": unit_price,
            "line_total": round(unit_price * qty, 2),
        })

    customer = body.get("customer", {})
    pickup_time = body.get("pickupTime") or (
        f"Delivery to {body['deliveryAddress']}" if body.get("deliveryAddress") else "ASAP"
    )

    # Prefer an explicit restaurant_id from the caller. Falls back to the legacy
    # Taqueria El Coral location-sniffing for older sites that never sent one.
    location = (body.get("location") or "").lower()
    if body.get("restaurant_id"):
        rid = body["restaurant_id"]
    elif "capitol" in location or "426" in location:
        rid = "taqueria_el_coral_capitol_expy"
    else:
        rid = "taqueria_el_coral_santa_teresa"

    order = {
        "order_id":               body.get("orderId", str(uuid.uuid4())),
        "restaurant_id":          rid,
        "timestamp":              body.get("timestamp", _pacific_now().isoformat()),
        "customer": {
            "name":        customer.get("name", ""),
            "phone":       customer.get("phone", ""),
            "pickup_time": pickup_time,
        },
        "order_type":             "standard",
        "items":                  items,
        "subtotal":               float(body.get("total", body.get("subtotal", 0))),
        "estimated_prep_minutes": PREP_TIME,
        "special_instructions":   body.get("notes", ""),
        "status":                 "confirmed",
        "call_sid":               "",
        "call_duration_seconds":  0,
        "source":                 body.get("source", "website"),
        "payment_method":         body.get("paymentMethod", ""),
        "payment_status":         (
            "paid_cash" if body.get("paymentMethod") == "cash" else
            "paid_card" if body.get("paymentMethod") == "card" else
            "unpaid"
        ),
    }

    asyncio.create_task(deliver_order(order))
    logger.info(f"Website order received | id={order['order_id']} | customer={customer.get('name')} | total=${order['subtotal']}")
    return {"status": "ok", "order_id": order["order_id"]}


@app.post("/api/orders/ingest")
async def api_ingest_order(request: Request):
    """
    Ingest an order from an external source (e.g. the restaurant chatbot).
    Secured by MAYA_INGEST_KEY header. Saves to SQLite and broadcasts to dashboard.
    """
    ingest_key = request.headers.get("X-Maya-Ingest-Key", "")
    expected_key = os.getenv("MAYA_INGEST_KEY", "")
    if expected_key and ingest_key != expected_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()

    # Support both nested {customer:{...}} and flat fields from the chatbot
    customer = body.get("customer") or {
        "name":        body.get("customer_name", ""),
        "phone":       body.get("customer_phone", ""),
        "pickup_time": body.get("pickup_time", "ASAP"),
    }

    order = {
        "order_id":               body.get("order_id", str(uuid.uuid4())),
        "restaurant_id":          body.get("restaurant_id", RESTAURANT_ID),
        "timestamp":              body.get("timestamp", _pacific_now().isoformat()),
        "customer":               customer,
        "order_type":             body.get("order_type", "standard"),
        "items":                  body.get("items", []),
        "subtotal":               float(body.get("subtotal", 0)),
        "estimated_prep_minutes": int(body.get("estimated_prep_minutes", PREP_TIME)),
        "special_instructions":   body.get("special_instructions", ""),
        "status":                 body.get("status", "confirmed"),
        "call_sid":               "",
        "call_duration_seconds":  0,
        "source":                 body.get("source", "chatbot"),
    }

    asyncio.create_task(deliver_order(order))
    logger.info(f"Order ingested | source={order['source']} | id={order['order_id']} | customer={customer.get('name')}")
    return {"status": "ok", "order_id": order["order_id"]}


@app.post("/api/orders/manual")
async def api_create_manual_order(request: Request, key: str = ""):
    """
    Staff-entered order from the employee dashboard (/staff).
    Covers two flows: In-Person Register (source=in_person) and a manually
    logged Phone call-in (source=phone) — both created by an employee, not
    an automated channel, so no ingest key — gated by DASHBOARD_PASSWORD instead.
    """
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    source = body.get("source", "in_person")
    if source not in ("in_person", "phone"):
        raise HTTPException(status_code=400, detail="source must be 'in_person' or 'phone'")

    items = body.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="items required")

    customer = body.get("customer") or {}
    subtotal = float(body.get("subtotal", sum(float(i.get("line_total", 0)) for i in items)))

    order = {
        "order_id":               str(uuid.uuid4()),
        "restaurant_id":          body.get("restaurant_id", RESTAURANT_ID),
        "timestamp":              _pacific_now().isoformat(),
        "customer": {
            "name":        customer.get("name", "Walk-in" if source == "in_person" else ""),
            "phone":       customer.get("phone", ""),
            "pickup_time": customer.get("pickup_time", "ASAP"),
        },
        "order_type":             "standard",
        "items":                  items,
        "subtotal":               subtotal,
        "estimated_prep_minutes": int(body.get("estimated_prep_minutes", PREP_TIME)),
        "special_instructions":   body.get("special_instructions", ""),
        "status":                 "confirmed",
        "call_sid":               "",
        "call_duration_seconds":  0,
        "source":                 source,
        "payment_status":         "unpaid",
    }

    await deliver_order(order)
    logger.info(f"Manual order created | source={source} | id={order['order_id']} | customer={order['customer']['name']}")
    return {"status": "ok", "order_id": order["order_id"], "order": order}


@app.get("/api/orders/{order_id}")
async def api_get_order(order_id: str):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENT API
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/payment/terminal/charge")
async def api_terminal_charge(request: Request):
    """Present charge to Stripe Terminal reader. Body: {order_id, reader_id?}"""
    body     = await request.json()
    order_id = body.get("order_id", "").strip()
    reader_id = (body.get("reader_id") or os.getenv("STRIPE_TERMINAL_READER_ID", "")).strip()

    if not order_id:
        raise HTTPException(status_code=400, detail="order_id required")
    if not reader_id:
        raise HTTPException(status_code=400, detail="STRIPE_TERMINAL_READER_ID not configured — add it to Railway env vars")

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    amount_cents = int(round(float(order.get("subtotal", 0)) * 100))
    if amount_cents <= 0:
        raise HTTPException(status_code=400, detail="Order total is $0 — cannot charge")

    try:
        result = await charge_terminal(order_id, amount_cents, reader_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Terminal charge error | order={order_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Terminal error: {e}")

    await update_order_payment(
        order_id,
        payment_status="processing",
        payment_method="card_terminal",
        stripe_payment_intent_id=result["payment_intent_id"],
    )
    asyncio.create_task(broadcast_payment_update(order_id, "processing", "card_terminal"))
    return result


@app.post("/api/payment/link/send")
async def api_payment_link(request: Request):
    """Create Stripe Checkout link and SMS it to the customer. Body: {order_id}"""
    body     = await request.json()
    order_id = body.get("order_id", "").strip()

    if not order_id:
        raise HTTPException(status_code=400, detail="order_id required")

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    amount_cents = int(round(float(order.get("subtotal", 0)) * 100))
    if amount_cents <= 0:
        raise HTTPException(status_code=400, detail="Order total is $0 — cannot charge")

    items = []
    try:
        items = json.loads(order.get("items_json", "[]"))
    except Exception:
        pass

    customer_name  = order.get("customer_name", "")
    customer_phone = order.get("customer_phone", "")

    try:
        link_result = await create_payment_link(order_id, amount_cents, items, customer_name)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Payment link error | order={order_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Payment link error: {e}")

    # SMS the link to the customer
    sms_sent = False
    if customer_phone:
        config = await load_menu(order.get("restaurant_id", RESTAURANT_ID))
        restaurant_name = config["restaurant_name"] if config else "the restaurant"
        sms_sent = await send_payment_sms(
            customer_phone,
            link_result["checkout_url"],
            float(order.get("subtotal", 0)),
            restaurant_name,
        )

    await update_order_payment(
        order_id,
        payment_status="link_sent",
        payment_method="payment_link",
        stripe_checkout_session_id=link_result["session_id"],
    )
    asyncio.create_task(broadcast_payment_update(order_id, "link_sent", "payment_link"))

    return {
        "checkout_url": link_result["checkout_url"],
        "sms_sent": sms_sent,
        "phone": customer_phone,
    }


@app.post("/api/payment/cash")
async def api_payment_cash(request: Request):
    """Mark order as paid with cash. Body: {order_id}"""
    body     = await request.json()
    order_id = body.get("order_id", "").strip()

    if not order_id:
        raise HTTPException(status_code=400, detail="order_id required")

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    await update_order_payment(order_id, payment_status="paid_cash", payment_method="cash")
    asyncio.create_task(broadcast_payment_update(order_id, "paid_cash", "cash"))
    logger.info(f"Cash payment recorded | order={order_id}")
    return {"status": "ok", "payment_status": "paid_cash"}


@app.post("/api/payment/square/charge")
async def api_square_charge(request: Request):
    """
    Charge an order via Square. STUBBED — process_square_payment() always
    mocks a successful charge until real Square credentials are wired up
    (see execution/square_payments.py). Body: {order_id}
    """
    body     = await request.json()
    order_id = body.get("order_id", "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id required")

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    result = await process_square_payment(order)

    await update_order_payment(
        order_id,
        payment_status="paid_square",
        payment_method="square",
        payment_reference_id=result.get("square_payment_id", ""),
    )
    asyncio.create_task(broadcast_payment_update(order_id, "paid_square", "square"))
    logger.info(f"Square payment recorded | order={order_id} | payment_id={result.get('square_payment_id')}")
    return {"status": "ok", "payment_status": "paid_square", "square_payment_id": result.get("square_payment_id")}


@app.post("/api/payment/webhook")
async def stripe_payment_webhook(request: Request):
    """Stripe webhook for payment confirmations (Terminal + Payment Link)."""
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = verify_payment_webhook(payload, sig_header)
    except ValueError as e:
        logger.error(f"Payment webhook signature invalid: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except (RuntimeError, Exception) as e:
        logger.warning(f"Payment webhook skipped: {e}")
        return {"received": True}

    event_type = event["type"]
    logger.info(f"Stripe payment event: {event_type}")

    if event_type == "checkout.session.completed":
        session  = event["data"]["object"]
        order_id = session.get("metadata", {}).get("order_id", "")
        if order_id:
            await update_order_payment(
                order_id,
                payment_status="paid_link",
                payment_method="payment_link",
                stripe_checkout_session_id=session.get("id", ""),
            )
            asyncio.create_task(broadcast_payment_update(order_id, "paid_link", "payment_link"))
            logger.info(f"Payment link completed | order={order_id}")

    elif event_type == "payment_intent.succeeded":
        intent   = event["data"]["object"]
        order_id = intent.get("metadata", {}).get("order_id", "")
        if order_id:
            method   = intent.get("payment_method_types", ["card"])[0]
            p_status = "paid_card" if "card_present" in intent.get("payment_method_types", []) else "paid_link"
            await update_order_payment(
                order_id,
                payment_status=p_status,
                payment_method=method,
                stripe_payment_intent_id=intent.get("id", ""),
            )
            asyncio.create_task(broadcast_payment_update(order_id, p_status, method))
            logger.info(f"PaymentIntent succeeded | order={order_id} | status={p_status}")

    return {"received": True}


@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success():
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:80px;background:#0f0f1a;color:#e8e8f0">
      <h1 style="color:#00d084;font-size:2.5rem">Payment received!</h1>
      <p style="font-size:1.2em;margin:20px 0">Thank you — your order is confirmed.</p>
      <p style="opacity:.6">You can close this window.</p>
    </body></html>""")


@app.get("/payment/cancel", response_class=HTMLResponse)
async def payment_cancel():
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:80px;background:#0f0f1a;color:#e8e8f0">
      <h1>Payment cancelled</h1>
      <p style="margin:20px 0;opacity:.7">No charge was made. Ask the staff to resend the link.</p>
    </body></html>""")


# ══════════════════════════════════════════════════════════════════════════════
# ONBOARDING
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/onboard", response_class=HTMLResponse)
async def onboard_page():
    path = Path(__file__).parent / "dashboard" / "onboard.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    raise HTTPException(status_code=404, detail="Onboarding page not found")


@app.post("/onboard/register")
async def onboard_register(request: Request):
    body          = await request.json()
    store_name    = body.get("store_name", "").strip()
    owner_email   = body.get("owner_email", "").strip()
    restaurant_id = body.get("restaurant_id", "").strip()
    manager_phone = body.get("manager_phone", MANAGER_PHONE).strip()
    prep_time     = int(body.get("prep_time", PREP_TIME))
    catering_thr  = float(body.get("catering_threshold", 150))
    twilio_phone  = body.get("twilio_phone", "").strip()
    hours         = body.get("hours", {})

    if not store_name or not owner_email or not restaurant_id:
        raise HTTPException(status_code=400,
                            detail="store_name, owner_email, and restaurant_id are required")

    config = {
        "restaurant_id":             restaurant_id,
        "restaurant_name":           store_name,
        "manager_phone":             manager_phone,
        "manager_email":             body.get("order_email", owner_email),
        "twilio_phone":              twilio_phone,
        "hours":                     {d: t for d, t in hours.items() if t is not None},
        "prep_time_estimate_minutes": prep_time,
        "catering_threshold":        {"min_dollars": catering_thr},
        "menu":                      [],
    }
    await save_menu(config)
    logger.info(f"Store registered | id={restaurant_id} | name={store_name} | email={owner_email}")
    asyncio.create_task(alert_slack(
        "New Store Signup 🏪",
        f"Store: {store_name}\nEmail: {owner_email}\nID: {restaurant_id}\nTwilio: {twilio_phone}",
        level="info",
    ))

    if STRIPE_ENABLED:
        try:
            url = create_checkout_session(store_name, owner_email, restaurant_id)
            return {"checkout_url": url, "restaurant_id": restaurant_id}
        except Exception as e:
            logger.error(f"Stripe checkout failed: {e}")

    return {"status": "registered", "restaurant_id": restaurant_id}


# ══════════════════════════════════════════════════════════════════════════════
# BILLING
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/subscribe")
async def subscribe(request: Request):
    body          = await request.json()
    store_name    = body.get("store_name", "").strip()
    owner_email   = body.get("owner_email", "").strip()
    restaurant_id = body.get("restaurant_id", "").strip()
    if not all([store_name, owner_email, restaurant_id]):
        raise HTTPException(status_code=400, detail="store_name, owner_email, restaurant_id required")
    try:
        url = create_checkout_session(store_name, owner_email, restaurant_id)
        return {"checkout_url": url}
    except Exception as e:
        logger.error(f"Stripe checkout failed: {e}")
        raise HTTPException(status_code=500, detail="Could not create checkout session")


@app.get("/billing/success", response_class=HTMLResponse)
async def billing_success():
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:80px;background:#0f0f1a;color:#e8e8f0">
      <h1 style="color:#00d084;font-size:2.5rem">🎉 You're in!</h1>
      <p style="font-size:1.2em;margin:20px 0">Your 30-day free trial has started.</p>
      <p>Check your email for next steps — upload your menu and Maya will start taking calls.</p>
      <p style="margin-top:40px"><a href="/" style="color:#e94560;font-size:.9rem">← View Dashboard</a></p>
    </body></html>""")


@app.get("/billing/cancel", response_class=HTMLResponse)
async def billing_cancel():
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:80px;background:#0f0f1a;color:#e8e8f0">
      <h1>No problem!</h1>
      <p style="margin:20px 0">You can start your free trial anytime.</p>
      <a href="/onboard" style="color:#e94560">← Back to signup</a>
    </body></html>""")


@app.post("/billing/webhook")
async def stripe_webhook(request: Request):
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        result = await handle_webhook(payload, sig_header)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if result["action"] == "past_due":
        asyncio.create_task(alert_slack("Payment Failed ⚠️",
            f"Account past due: {result.get('email')}", level="warning"))
    elif result["action"] == "cancelled":
        asyncio.create_task(alert_slack("Subscription Cancelled",
            f"Cancelled: {result.get('email')}", level="warning"))
    elif result["action"] == "activated":
        asyncio.create_task(alert_slack("Store Activated ✅",
            f"Email: {result.get('email')} | ID: {result.get('restaurant_id')}", level="info"))
    return {"received": True}


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD WebSocket + UI
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket, key: str = ""):
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        await websocket.close(code=4003)
        return
    await websocket.accept()
    dashboard_clients.add(websocket)
    logger.info(f"Dashboard connected | total={len(dashboard_clients)}")
    try:
        # Send all current orders on connect
        orders = await get_orders(limit=300)
        await websocket.send_text(json.dumps({"event": "init", "orders": orders}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        dashboard_clients.discard(websocket)
        logger.info(f"Dashboard disconnected | total={len(dashboard_clients)}")


_LOGIN_FORM = """<!doctype html><html><head><meta charset="utf-8">
<title>Maya Dashboard — Login</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .box{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:40px 36px;width:320px;text-align:center}
  h1{color:#f8fafc;font-size:18px;margin:0 0 6px}
  p{color:#94a3b8;font-size:13px;margin:0 0 24px}
  input{width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #475569;border-radius:8px;color:#f8fafc;font-size:14px;padding:10px 14px;margin-bottom:14px;outline:none}
  input:focus{border-color:#6366f1}
  button{width:100%;background:#6366f1;color:#fff;border:none;border-radius:8px;padding:11px;font-size:14px;font-weight:600;cursor:pointer}
  button:hover{background:#4f46e5}
  .err{color:#f87171;font-size:12px;margin-top:10px}
</style></head><body>
<div class="box">
  <h1>Maya Dashboard</h1>
  <p>Enter your dashboard password to continue.</p>
  <form method="get">
    <input type="password" name="key" placeholder="Password" autofocus autocomplete="current-password">
    <button type="submit">Enter</button>
  </form>
  {error}
</div></body></html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard(key: str = ""):
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        error = '<p class="err">Incorrect password.</p>' if key else ''
        return HTMLResponse(_LOGIN_FORM.replace("{error}", error), status_code=401 if key else 200)
    path = Path(__file__).parent / "dashboard" / "index.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse("<h1>Maya 2.0 Dashboard</h1><p>Dashboard file not found.</p>")


@app.get("/kitchen", response_class=HTMLResponse)
async def kitchen_display(key: str = ""):
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        error = '<p class="err">Incorrect password.</p>' if key else ''
        return HTMLResponse(_LOGIN_FORM.replace("{error}", error), status_code=401 if key else 200)
    path = Path(__file__).parent / "dashboard" / "kitchen.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse("<h1>Kitchen Display</h1><p>kitchen.html not found.</p>")


@app.get("/revenue", response_class=HTMLResponse)
async def revenue_page(key: str = ""):
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        error = '<p class="err">Incorrect password.</p>' if key else ''
        return HTMLResponse(_LOGIN_FORM.replace("{error}", error), status_code=401 if key else 200)
    path = Path(__file__).parent / "dashboard" / "revenue.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse("<h1>Revenue Tracker</h1><p>revenue.html not found.</p>")


@app.get("/api/revenue")
async def api_revenue(key: str = ""):
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    from execution.order_store import get_revenue_stats
    return JSONResponse(await get_revenue_stats())


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(key: str = ""):
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        error = '<p class="err">Incorrect password.</p>' if key else ''
        return HTMLResponse(_LOGIN_FORM.replace("{error}", error), status_code=401 if key else 200)
    path = Path(__file__).parent / "dashboard" / "admin.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse("<h1>Admin</h1><p>admin.html not found.</p>")


@app.get("/staff", response_class=HTMLResponse)
async def staff_page(key: str = ""):
    """Employee-facing dashboard — Orders (online + called-in) and In-Person (Active + Register)."""
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        error = '<p class="err">Incorrect password.</p>' if key else ''
        return HTMLResponse(_LOGIN_FORM.replace("{error}", error), status_code=401 if key else 200)
    path = Path(__file__).parent / "dashboard" / "staff.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse("<h1>Staff Dashboard</h1><p>staff.html not found.</p>")


@app.get("/kds", response_class=HTMLResponse)
async def kds_page(key: str = ""):
    """Kitchen Display System — In Progress (paid + unpaid) and Completed queues."""
    if DASHBOARD_PASSWORD and key != DASHBOARD_PASSWORD:
        error = '<p class="err">Incorrect password.</p>' if key else ''
        return HTMLResponse(_LOGIN_FORM.replace("{error}", error), status_code=401 if key else 200)
    path = Path(__file__).parent / "dashboard" / "kds.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse("<h1>Kitchen Display</h1><p>kds.html not found.</p>")


@app.get("/order", response_class=HTMLResponse)
async def order_page():
    """Customer-facing online order form — no auth, no chatbot."""
    path = Path(__file__).parent / "dashboard" / "order.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse("<h1>Order page not found.</h1>", status_code=404)


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _resolve_restaurant(to_phone: str) -> dict:
    """Resolve restaurant config from the Twilio number that was called."""
    if to_phone:
        from execution.order_store import load_menu_by_phone
        config = await load_menu_by_phone(to_phone)
        if config:
            return config
    # Fall back to env var RESTAURANT_ID
    config = await load_menu(RESTAURANT_ID)
    return config or _default_config()


async def _resolve_restaurant_from_call(call_sid: str) -> dict:
    """Re-load restaurant config using call_sid (from cached state)."""
    state = get_state(call_sid)
    if state and state.get("restaurant_id"):
        config = await load_menu(state["restaurant_id"])
        if config:
            return config
    return await load_menu(RESTAURANT_ID) or _default_config()


async def _submit_order(state: dict, config: dict, status: str):
    call_sid = state.get("call_sid", "")
    try:
        loop     = asyncio.get_running_loop()
        now      = loop.time()
        duration = int(now - _call_start.get(call_sid, now))

        order = {
            "order_id":               state.get("order_id", str(uuid.uuid4())),
            "restaurant_id":          state.get("restaurant_id", RESTAURANT_ID),
            "timestamp":              _pacific_now().isoformat(),
            "customer":               state.get("customer", {}),
            "order_type":             "catering" if state.get("stage") == "catering" else "standard",
            "items":                  state.get("items", []),
            "subtotal":               state.get("subtotal", 0.0),
            "estimated_prep_minutes": config.get("prep_time_estimate_minutes", PREP_TIME),
            "special_instructions":   state.get("special_instructions", ""),
            "status":                 status,
            "call_sid":               call_sid,
            "call_duration_seconds":  duration,
            "source":                 "voice",
        }
        logger.info(f"Saving order | id={order['order_id']} | items={len(order['items'])} | subtotal=${order['subtotal']:.2f} | customer={order['customer']}")
        results = await deliver_order(order)
        logger.info(f"Order delivered | id={order['order_id']} | {results}")
    except Exception as e:
        logger.error(f"_submit_order FAILED | CallSid={call_sid} | {type(e).__name__}: {e}", exc_info=True)
    finally:
        clear_state(call_sid)


async def _call_timeout_task(call_sid: str, config: dict):
    """
    Hard 5-minute call limit.
    If an order is actively in progress at the 5-min mark, give a 60-second
    grace period with a wrap-up warning instead of cutting immediately.
    """
    await asyncio.sleep(300)   # 5 minutes
    logger.warning(f"5-min timeout | CallSid={call_sid}")

    state           = get_state(call_sid)
    lang            = state.get("detected_language", "en") if state else "en"
    manager_phone   = config.get("manager_phone", MANAGER_PHONE)
    restaurant_name = config.get("restaurant_name", "the restaurant")

    # If the caller has items in their cart but hasn't confirmed yet,
    # give a 60-second grace period so the order isn't lost.
    if state and state.get("items") and state.get("stage") not in ("submit", "submitted", "escalated"):
        logger.info(f"Order in progress at timeout — giving 60s grace | CallSid={call_sid}")
        grace_msg = (
            f"Just a heads-up — we're coming up on our time limit. "
            f"Let's wrap up your order quickly, or I can connect you to the team at "
            f"{_format_phone(manager_phone)}."
        )
        resp = VoiceResponse()
        audio = await try_synthesize(grace_msg)
        if audio:
            resp.play(audio)
        else:
            _say(resp, grace_msg, lang)
        _make_gather(resp, timeout=60, language=lang)
        try:
            _get_twilio().calls(call_sid).update(twiml=str(resp))
        except Exception:
            pass
        await asyncio.sleep(60)   # grace period — normal flow handles the rest

    # Hard cut — transfer to manager
    state = get_state(call_sid)
    if state and state.get("stage") in ("submit", "submitted", "escalated"):
        # Order already confirmed during grace period — nothing to do
        _cleanup_call(call_sid)
        return

    msg  = generate_timeout_response(restaurant_name, manager_phone, language=lang)
    resp = VoiceResponse()
    audio = await try_synthesize(msg)
    if audio:
        resp.play(audio)
    else:
        _say(resp, msg, lang)
    resp.dial(_to_e164(manager_phone))

    try:
        _get_twilio().calls(call_sid).update(twiml=str(resp))
    except Exception as e:
        msg_str = str(e).lower()
        if "not in-progress" in msg_str or "not in progress" in msg_str:
            logger.info(f"Timeout fired but call already ended | CallSid={call_sid}")
        else:
            logger.error(f"Failed to inject timeout TwiML: {e}")

    if state and state.get("items"):
        await _submit_order(state, config, "escalated")

    _cleanup_call(call_sid)


def _cleanup_call(call_sid: str):
    t = _call_timers.pop(call_sid, None)
    if t:
        t.cancel()
    _call_start.pop(call_sid, None)


def _twiml(content: str):
    return HTMLResponse(content=content, media_type="application/xml")


def _default_config() -> dict:
    all_day = {"open": "00:00", "close": "23:59"}
    return {
        "restaurant_id":             RESTAURANT_ID,
        "restaurant_name":           "the restaurant",
        "manager_phone":             MANAGER_PHONE,
        "hours":                     {d: all_day for d in
                                      ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]},
        "prep_time_estimate_minutes": PREP_TIME,
        "catering_threshold":        {"min_dollars": 150},
        "menu":                      [],
    }


def _format_phone(number: str) -> str:
    n = number.replace("-","").replace(" ","").replace("+1","")
    if len(n) == 10:
        return f"{n[0]}-{n[1]}-{n[2]}, {n[3]}-{n[4]}-{n[5]}, {n[6]}-{n[7]}-{n[8]}-{n[9]}"
    return number


def _to_e164(number: str) -> str:
    n = number.replace("-","").replace(" ","")
    if not n.startswith("+"):
        n = "+1" + n.replace("+1","")
    return n


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting Maya 2.0 on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
