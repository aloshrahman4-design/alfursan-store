"""Telegram bot entry point: supplier photo(s) in -> priced, approved post(s) out.

Pipeline (kept in separate modules on purpose -- see each file's docstring
for why -- so nothing "hallucinates" numbers or pixels):
  1. gemini_extractor.py  Gemini Vision reads + localizes the supplier's prices into strict JSON.
  2. pricing.py            Plain Python applies the markup arithmetic.
  3. image_processor.py    Pillow patches only the price digits, wherever they are.
  4. Admin approves via an inline button -> bot posts to the channel.

Batching: admins commonly send a whole album (Telegram "media group") of up
to 10 photos at once, with ONE caption carrying the markup for the whole
batch. Telegram delivers each photo in an album as a SEPARATE update, so
incoming photos are buffered per media_group_id and flushed together after
a short quiet period (see _BATCH_DEBOUNCE_SECONDS). A lone photo is just
a batch of one, processed on the same path with a near-zero debounce.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    Update,
)
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
from pricing import (
    MarkupParseError,
    PackQuantityError,
    compute_prices,
    parse_markup,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("alfursan-bot")

_BATCH_DEBOUNCE_SECONDS = 2.0
_MAX_CONCURRENT_EXTRACTIONS = 3

# Posts waiting for admin approval, keyed by a short id (Telegram's
# callback_data is capped at 64 bytes so the payload can't live there).
_PENDING: Dict[str, Dict[str, Any]] = {}
_PENDING_TTL_SECONDS = 3600


@dataclass
class _Batch:
    chat_id: int
    messages: List[Message] = field(default_factory=list)
    caption: Optional[str] = None


# Photos waiting for their album (or the single-photo debounce) to settle.
_BATCHES: Dict[str, _Batch] = {}


def _is_authorized(user_id: int) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    return user_id in config.ALLOWED_USER_IDS


def _remember_batch(items: List[Dict[str, Any]]) -> str:
    _cleanup_pending()
    pending_id = uuid.uuid4().hex[:12]
    _PENDING[pending_id] = {"items": items, "created_at": time.time()}
    return pending_id


def _cleanup_pending() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING.items() if now - v["created_at"] > _PENDING_TTL_SECONDS]
    for k in expired:
        _PENDING.pop(k, None)


async def _send_processed_photos(bot, chat_id, items: List[Dict[str, Any]]) -> None:
    """Send processed photos back, chunked to Telegram's 10-per-album limit.
    sendMediaGroup requires >=2 items, so a lone photo (or a final leftover
    chunk of 1) falls back to a plain send_photo.
    """
    for start in range(0, len(items), 10):
        chunk = items[start : start + 10]
        if len(chunk) == 1:
            item = chunk[0]
            await bot.send_photo(chat_id, photo=item["image"], caption=item["caption"])
        else:
            media = [InputMediaPhoto(media=it["image"], caption=it["caption"]) for it in chunk]
            await bot.send_media_group(chat_id, media)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلاً 👋\n"
        "أرسل صورة منتج واحدة أو ألبوم كامل (حتى 10 صور دفعة واحدة) مع كتابة "
        "قيمة الزيادة في الكابشن -- تُضاف على مجموع سعر الكارتونة لكل منتج ثم "
        "يُقسَّم الناتج على عدد القطع لاستخراج سعر المفرد الجديد. مثال:\n"
        "+10000  أو  15%\n\n"
        "التعبئة تُقرأ تلقائياً سواء كُتبت كعدد قطع (24، 18) أو كعدد درزنات "
        "عشري (1.5 = 18 قطعة، 1.25 = 15 قطعة)."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user

    if user is None or not _is_authorized(user.id):
        await message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    group_key = message.media_group_id or f"single-{message.message_id}"
    batch = _BATCHES.setdefault(group_key, _Batch(chat_id=message.chat_id))
    batch.messages.append(message)
    if message.caption:
        batch.caption = message.caption

    job_name = f"flush:{group_key}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    delay = _BATCH_DEBOUNCE_SECONDS if message.media_group_id else 0.1
    context.job_queue.run_once(_flush_batch, when=delay, name=job_name, data=group_key, chat_id=message.chat_id)


async def _process_one(
    index: int, message: Message, markup
) -> Dict[str, Any]:
    try:
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())

        product = await asyncio.to_thread(extract_product_data, image_bytes)
        prices = compute_prices(product, markup)
        processed_image, fully_patched = await asyncio.to_thread(
            process_image, image_bytes, product, prices
        )
        caption_text = build_caption(product, prices)
        if not fully_patched:
            caption_text += "\n\n⚠️ لم يتم تحديد موقع أحد السعرين بدقة في الصورة، تحقق يدوياً قبل النشر."

        return {
            "ok": True,
            "index": index,
            "image": processed_image,
            "caption": caption_text,
            "model_code": product.model_code,
        }
    except (ExtractionError, PackQuantityError) as exc:
        return {"ok": False, "index": index, "error": str(exc)}
    except Exception:
        logger.exception("Batch item %s failed", index)
        return {"ok": False, "index": index, "error": "خطأ غير متوقع أثناء المعالجة."}


async def _flush_batch(context: ContextTypes.DEFAULT_TYPE) -> None:
    group_key = context.job.data
    batch = _BATCHES.pop(group_key, None)
    if batch is None or not batch.messages:
        return

    chat_id = batch.chat_id
    messages = batch.messages

    try:
        markup = parse_markup(batch.caption)
    except MarkupParseError as exc:
        await context.bot.send_message(chat_id, str(exc))
        return

    status_msg = await context.bot.send_message(
        chat_id,
        f"⏳ جارِ معالجة {len(messages)} صورة..." if len(messages) > 1 else "⏳ جارِ استخراج البيانات وتجهيز المنشور...",
    )

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_EXTRACTIONS)

    async def _guarded(index: int, message: Message) -> Dict[str, Any]:
        async with semaphore:
            return await _process_one(index, message, markup)

    results = await asyncio.gather(*(_guarded(i, m) for i, m in enumerate(messages, start=1)))

    successes = [r for r in results if r["ok"]]
    failures = [r for r in results if not r["ok"]]

    await status_msg.delete()

    if not successes:
        fail_lines = "\n".join(f"❌ صورة {r['index']}: {r['error']}" for r in failures)
        await context.bot.send_message(chat_id, f"تعذّرت معالجة كل الصور في هذه الدفعة:\n{fail_lines}")
        return

    pending_id = _remember_batch(
        [{"image": r["image"], "caption": r["caption"]} for r in successes]
    )

    await _send_processed_photos(context.bot, chat_id, successes)

    summary_lines = [f"✅ تمت معالجة {len(successes)} من {len(messages)} صورة بنجاح."]
    if failures:
        summary_lines.append("")
        summary_lines.append("⚠️ تعذّرت معالجة:")
        summary_lines.extend(f"  صورة {r['index']}: {r['error']}" for r in failures)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ نشر الكل للقناة ({len(successes)})", callback_data=f"publish:{pending_id}"
                ),
                InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel:{pending_id}"),
            ]
        ]
    )
    await context.bot.send_message(chat_id, "\n".join(summary_lines), reply_markup=keyboard)


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
        await query.edit_message_text("⚠️ انتهت صلاحية هذه الدفعة، أرسل الصور مجدداً.")
        return

    items = pending["items"]

    if action == "publish":
        await _send_processed_photos(context.bot, config.CHANNEL_ID, items)
        _PENDING.pop(pending_id, None)
        await query.edit_message_text(f"✅ تم نشر {len(items)} منتج في القناة.")
    elif action == "cancel":
        _PENDING.pop(pending_id, None)
        await query.edit_message_text("❌ تم إلغاء هذه الدفعة.")


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
