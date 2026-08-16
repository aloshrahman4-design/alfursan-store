"""Central configuration, loaded from environment variables (.env)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Target channel for the "publish" button: "@channel_username" or "-100xxxxxxxxxx"
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

# Comma-separated Telegram user IDs allowed to use the bot.
# Leave empty during local testing to allow everyone (NOT recommended in production).
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

FONT_REGULAR_PATH = os.getenv("FONT_REGULAR_PATH", "fonts/Cairo-Regular.ttf")
FONT_BOLD_PATH = os.getenv("FONT_BOLD_PATH", "fonts/Cairo-Bold.ttf")

# Fraction of the image height, measured from the bottom, treated as the
# supplier's footer strip. Proportional (not raw pixels) so the bot keeps
# working across suppliers who don't all export the same resolution.
FOOTER_HEIGHT_RATIO = float(os.getenv("FOOTER_HEIGHT_RATIO", "0.22"))


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
    for path in (FONT_REGULAR_PATH, FONT_BOLD_PATH):
        if not os.path.isfile(path):
            raise RuntimeError(
                f"Font file not found: {path}. Download an Arabic-capable TTF "
                "(e.g. Cairo or Noto Naskh Arabic) and place it there, or set "
                "FONT_REGULAR_PATH/FONT_BOLD_PATH in .env."
            )
