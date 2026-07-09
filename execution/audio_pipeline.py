"""
audio_pipeline.py — ElevenLabs TTS + Deepgram STT (Maya 2.0)
ElevenLabs is used when USE_ELEVENLABS=true (env). Falls back to Polly on timeout or error.
Deepgram handles speech transcription and language detection.
"""

import os
import uuid
import asyncio
import logging
import httpx
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

logger = logging.getLogger(__name__)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
DEEPGRAM_API_KEY   = os.getenv("DEEPGRAM_API_KEY", "")
BASE_URL           = os.getenv("BASE_URL", "http://localhost:8000")
USE_ELEVENLABS     = os.getenv("USE_ELEVENLABS", "true").lower() == "true"
EL_TIMEOUT_SECS    = float(os.getenv("ELEVENLABS_TIMEOUT_SECS", "1.0"))  # flash v2.5 typically <400ms; fall back to instant Polly if slower

# Maya's ElevenLabs voice — Rachel (warm American female). Override via ELEVENLABS_VOICE_ID.
MAYA_VOICE_ID   = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
VOICE_SETTINGS  = {
    "stability":        0.5,
    "similarity_boost": 0.8,
    "style":            0.3,
    "use_speaker_boost": True,
}

# ── Language routing tables ───────────────────────────────────────────────────

# ISO 639-1 → Twilio <Gather> language attribute
# Controls which ASR engine Twilio uses — major accuracy improvement for non-English.
TWILIO_LANGUAGE_MAP: dict[str, str] = {
    "en": "en-US",
    "es": "es-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-BR",
    "zh": "zh-CN",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "ar": "ar-AE",
    "hi": "hi-IN",
    "ru": "ru-RU",
    "pl": "pl-PL",
    "nl": "nl-NL",
}

# ISO 639-1 → (Polly neural voice name, Polly language code) for TTS fallback
POLLY_VOICE_MAP: dict[str, tuple[str, str]] = {
    "en": ("Polly.Joanna-Neural", "en-US"),
    "es": ("Polly.Lupe-Neural",   "es-US"),
    "fr": ("Polly.Lea-Neural",    "fr-FR"),
    "de": ("Polly.Vicki-Neural",  "de-DE"),
    "it": ("Polly.Bianca-Neural", "it-IT"),
    "pt": ("Polly.Vitoria-Neural","pt-BR"),
    "zh": ("Polly.Zhiyu-Neural",  "cmn-CN"),
    "ja": ("Polly.Takumi-Neural", "ja-JP"),
    "ko": ("Polly.Seoyeon-Neural","ko-KR"),
    "nl": ("Polly.Laura-Neural",  "nl-NL"),
    "hi": ("Polly.Aditi",         "hi-IN"),
    "ar": ("Polly.Zeina",         "arb"),
    "ru": ("Polly.Tatyana",       "ru-RU"),
    "pl": ("Polly.Ewa",           "pl-PL"),
}


def get_polly_voice(language: str) -> tuple[str, str]:
    """Return (voice_name, language_code) for Polly <Say> TTS fallback."""
    return POLLY_VOICE_MAP.get(language, POLLY_VOICE_MAP["en"])


def get_twilio_language(language: str) -> str:
    """Return Twilio <Gather> language code for the detected caller language."""
    return TWILIO_LANGUAGE_MAP.get(language, "en-US")

# AUDIO_DIR can be overridden via env var for Railway Volume persistence.
_audio_dir_env = os.getenv("AUDIO_DIR", "")
TMP_DIR = Path(_audio_dir_env) if _audio_dir_env else Path(__file__).resolve().parent.parent / ".tmp" / "audio"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# Simple in-memory cache to avoid re-synthesizing identical phrases
_phrase_cache: dict[str, str] = {}


# ── ElevenLabs TTS ────────────────────────────────────────────────────────────

async def synthesize_speech(text: str, filename: str = None) -> str:
    """
    Convert text to speech with ElevenLabs.
    Returns a public URL to the .mp3 that Twilio can fetch.
    Raises on failure — caller should catch and fall back to Polly <Say>.
    """
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    cache_key = f"{MAYA_VOICE_ID}:{text}"
    if cache_key in _phrase_cache:
        return _phrase_cache[cache_key]

    if not filename:
        filename = f"{uuid.uuid4()}.mp3"

    filepath = TMP_DIR / filename
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{MAYA_VOICE_ID}"
    headers = {
        "xi-api-key":   ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept":       "audio/mpeg",
    }
    payload = {
        "text":       text,
        # eleven_flash_v2_5: ElevenLabs' fastest model (~200-500ms vs ~2s for multilingual).
        # Critical for sub-2-second response targets. Falls back to Polly on any failure.
        "model_id":   "eleven_flash_v2_5",
        "voice_settings": VOICE_SETTINGS,
    }

    async with httpx.AsyncClient(timeout=EL_TIMEOUT_SECS + 1) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        filepath.write_bytes(response.content)

    public_url = f"{BASE_URL}/audio/{filename}"
    _phrase_cache[cache_key] = public_url
    return public_url


async def try_synthesize(text: str) -> str | None:
    """
    Attempt ElevenLabs synthesis with a timeout.
    Returns the audio URL on success, None on any failure.
    Caller falls back to Polly <Say> when None is returned.
    """
    if not USE_ELEVENLABS or not ELEVENLABS_API_KEY:
        return None
    try:
        return await asyncio.wait_for(synthesize_speech(text), timeout=EL_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        logger.warning(f"ElevenLabs timeout ({EL_TIMEOUT_SECS}s) — falling back to Polly")
        return None
    except Exception as e:
        logger.warning(f"ElevenLabs error — falling back to Polly: {e}")
        return None


# ── Deepgram STT ──────────────────────────────────────────────────────────────

async def transcribe_audio(audio_bytes: bytes, language: str = "en",
                          content_type: str = "audio/wav") -> str:
    """
    Transcribe audio bytes via Deepgram nova-3 REST API.

    Key params for restaurant call quality:
    - nova-3          : best accent/mumble accuracy (2024+ model)
    - numerals=true   : "six six nine two four eight..." → "669248..." (critical for phone numbers)
    - smart_format    : formats times, dates, currency naturally
    - no_delay        : lower latency for short utterances
    """
    if not DEEPGRAM_API_KEY:
        return ""
    url = "https://api.deepgram.com/v1/listen"
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type":  content_type,
    }
    params = {
        "model":        "nova-3",
        "language":     language,
        "smart_format": "true",
        "numerals":     "true",   # spoken digits → numeric characters
        "punctuate":    "false",  # not needed; keeps output clean
        "diarize":      "false",  # single speaker — faster
        "utterances":   "false",  # faster without utterance segments
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, params=params, content=audio_bytes)
        response.raise_for_status()
        data = response.json()
    try:
        return data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    except (KeyError, IndexError):
        return ""


async def detect_language(audio_bytes: bytes) -> str:
    """Detect caller's language via Deepgram. Defaults to 'en' on failure."""
    if not DEEPGRAM_API_KEY:
        return "en"
    url = "https://api.deepgram.com/v1/listen"
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type":  "audio/wav",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=headers,
                                  params={"model": "nova-2", "detect_language": "true"},
                                  content=audio_bytes)
            r.raise_for_status()
            return r.json()["results"]["channels"][0].get("detected_language", "en")
    except Exception:
        return "en"


# ── Startup Pre-warm ──────────────────────────────────────────────────────────

async def prewarm_audio_cache(restaurant_name: str):
    """Pre-generate common Maya phrases at startup to reduce first-call latency."""
    if not USE_ELEVENLABS or not ELEVENLABS_API_KEY:
        logger.info("ElevenLabs disabled — skipping audio pre-warm")
        return {}

    prep = os.getenv("PREP_TIME_MINUTES", "10")
    phrases = {
        "silence_prompt":  "I'm still here — what can I get started for you?",
        "repeat_prompt":   "Sorry, I didn't quite catch that — could you say that one more time?",
        "get_name":        "Can I get your name for the order?",
        "get_phone":       "And the best phone number to reach you?",
        "get_pickup":      "When would you like to pick that up?",
        "closing":         f"Perfect, your order's all confirmed! See you in about {prep} minutes.",
    }

    cache = {}
    for key, text in phrases.items():
        try:
            url = await synthesize_speech(text, filename=f"cache_{key}.mp3")
            cache[key] = url
            logger.info(f"Audio pre-warm OK: {key}")
        except Exception as e:
            logger.warning(f"Audio pre-warm failed ({key}): {e}")
            cache[key] = None

    return cache
