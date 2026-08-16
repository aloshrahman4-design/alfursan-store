"""Central configuration, loaded from environment variables (.env)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# "-latest" is a Google-maintained alias that always points at their current
# default fast multimodal model, so this keeps working without anyone having
# to notice a model was renamed/retired and go edit .env. GEMINI_FALLBACK_MODELS
# are tried in order if the primary model's call fails for any reason (see
# gemini_extractor._model_candidates) -- belt-and-suspenders against the
# exact "this model doesn't exist anymore" failure that prompted this.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash,gemini-2.0-flash").split(",")
    if m.strip()
]

# Target channel for the "publish" button: "@channel_username" or "-100xxxxxxxxxx"
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

# Comma-separated Telegram user IDs allowed to use the bot.
# Leave empty during local testing to allow everyone (NOT recommended in production).
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

# Used to draw the new price digits patched into the image (image_processor.py).
# Only digits/commas are ever drawn onto images -- Arabic labels live in the
# Telegram caption text, which Telegram renders natively -- so any TTF with
# numeral glyphs works; a bold weight just looks best for a price callout.
FONT_BOLD_PATH = os.getenv("FONT_BOLD_PATH", "fonts/Cairo-Bold.ttf")

# Append-only JSON Lines audit trail of every published item (see audit_log.py).
AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "audit_log.jsonl")


def validate() -> None:
    """Fail fast and loudly on startup instead of on the first incoming photo."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not CHANNEL_ID:
        missing.append("CHANNEL_ID")
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing) +
            ". Copy .env.example to .env and fill them in."
        )
    if not os.path.isfile(FONT_BOLD_PATH):
        raise RuntimeError(
            f"Font file not found: {FONT_BOLD_PATH}. Download a bold TTF "
            "(e.g. Cairo-Bold) and place it there, or set FONT_BOLD_PATH in .env."
        )
