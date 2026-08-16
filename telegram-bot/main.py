"""Telegram bot entry point: supplier photo in -> priced, approved post out.

Pipeline (kept in separate modules on purpose -- see each file's docstring
for why -- so nothing "hallucinates" numbers or pixels):
  1. gemini_extractor.py  Gemini Vision reads the supplier's footer text into strict JSON.
  2. pricing.py            Plain Python applies the markup arithmetic.
  3. image_processor.py    Pillow overwrites only the footer strip with the new numbers.
  4. Admin approves via an inline button -> bot posts to the channel.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from gemini_extractor import ExtractionError, extract_product_data
from image_processor import build_caption, process_image
from pricing import MarkupParseError, compute_prices, parse_markup

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("alfursan-bot")

# Posts waiting for admin approval, keyed by a short id (Telegram's
# callback_data is capped at 64 bytes so the payload can't live there).
_PENDING: Dict[str, Dict[str, Any]] = {}
_PENDING_TTL_SECONDS = 3600


def _remember(image_bytes: bytes, caption: str) -> str:
    _cleanup_pending()
    pending_id = uuid.uuid4().hex[:12]
    _PENDING[pending_id] = {
        "image": image_bytes,
        "caption": caption,
        "created_at": time.time(),
    }
    return pending_id


def _cleanup_pending() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING.items() if now - v["created_at"] > _PENDING_TTL_SECONDS]
    for k in expired:
        _PENDING.pop(k, None)


def _is_authorized(user_id: int) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    return user_id in config.ALLOWED_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلاً 👋\n"
        "أرسل صورة المنتج مع كتابة قيمة الزيادة في الكابشن، مثال:\n"
        "+1000  أو  15%"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user

    if user is None or not _is_authorized(user.id):
        await message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    try:
        markup = parse_markup(message.caption)
    except MarkupParseError as exc:
        await message.reply_text(str(exc))
        return

    status_msg = await message.reply_text("⏳ جارِ استخراج البيانات وتجهيز المنشور...")

    try:
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())

        product = await asyncio.to_thread(extract_product_data, image_bytes)
        prices = compute_prices(product, markup)
        processed_image = await asyncio.to_thread(process_image, image_bytes, product, prices)
        post_caption = build_caption(product, prices)

    except ExtractionError as exc:
        logger.warning("Extraction failed: %s", exc)
        await status_msg.edit_text(
            "❌ تعذّر قراءة بيانات المورد من الصورة. تأكد أن الشريط السفلي واضح وحاول مجدداً."
        )
        return
    except Exception:
        logger.exception("Failed to process incoming product photo")
        await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء المعالجة. حاول مجدداً.")
        return

    pending_id = _remember(processed_image, post_caption)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ نشر للقناة", callback_data=f"publish:{pending_id}"),
                InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel:{pending_id}"),
            ]
        ]
    )

    await status_msg.delete()
    await message.reply_photo(photo=processed_image, caption=post_caption, reply_markup=keyboard)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if user is None or not _is_authorized(user.id):
        await query.answer("غير مصرح لك.", show_alert=True)
        return

    await query.answer()

    action, _, pending_id = (query.data or "").partition(":")
    pending = _PENDING.get(pending_id)

    if pending is None:
        await query.edit_message_caption(caption="⚠️ انتهت صلاحية هذا المنشور، أرسل الصورة مجدداً.")
        return

    if action == "publish":
        await context.bot.send_photo(
            chat_id=config.CHANNEL_ID,
            photo=pending["image"],
            caption=pending["caption"],
        )
        _PENDING.pop(pending_id, None)
        await query.edit_message_caption(caption=f"{pending['caption']}\n\n✅ تم النشر في القناة.")
    elif action == "cancel":
        _PENDING.pop(pending_id, None)
        await query.edit_message_caption(caption=f"{pending['caption']}\n\n❌ تم الإلغاء.")


def main() -> None:
    config.validate()

    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot starting (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
