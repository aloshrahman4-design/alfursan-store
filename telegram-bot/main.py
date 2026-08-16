"""Telegram bot entry point: supplier photo(s) in -> priced, approved post(s) out.

Pipeline (kept in separate modules on purpose -- see each file's docstring
for why -- so nothing "hallucinates" numbers or pixels):
  1. gemini_extractor.py  Gemini Vision reads + localizes the supplier's prices into strict JSON.
  2. pricing.py            Plain Python applies the markup arithmetic and cross-checks the two prices.
  3. image_processor.py    Pillow patches only the price digits, wherever they are.
  4. Admin approves via an inline button -> bot posts to the channel.
  5. audit_log.py          Every publish is appended to a local audit trail.

Batching: admins commonly send a whole album (Telegram "media group") of up
to 10 photos at once, with ONE caption carrying the markup for the whole
batch. Telegram delivers each photo in an album as a SEPARATE update, so
incoming photos are buffered per media_group_id and flushed together after
a short quiet period (see _BATCH_DEBOUNCE_SECONDS). A lone photo is just
a batch of one, processed on the same path with a near-zero debounce.

Reliability: an item whose supplier-printed dozen/single prices disagree
by more than pricing._MISMATCH_TOLERANCE is still previewed (so nothing
blocks silently) but excluded from "publish all" until either fixed by
replying to that specific photo with a correction (see handle_correction)
or the batch is republished after a fix.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

import audit_log
import config
from gemini_extractor import ExtractionError, ProductData, extract_product_data
from image_processor import build_caption, process_image
from pricing import (
    MarkupParseError,
    MarkupSpec,
    PackQuantityError,
    PriceResult,
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
_PENDING_TTL_SECONDS = 3600

_CORRECTION_FIELD_MAP = {
    "مفرد": "single_price",
    "قطعة": "single_price",
    "درزن": "dozen_price",
    "دزن": "dozen_price",
    "كود": "model_code",
    "قياس": "sizes",
    "قياسات": "sizes",
    "تعبئة": "pack_quantity_raw",
}

# Batches waiting for admin approval: pending_id -> {"items": {item_id: item}, "created_at": ts}.
# item_id (a small string) is stable for the batch's lifetime so a reply
# correction can always find the right item even after other items in the
# same batch have already been published.
_PENDING: Dict[str, Dict[str, Any]] = {}

# Maps a sent preview photo's Telegram message_id -> (pending_id, item_id),
# so a text reply to that specific photo can be routed to the right item.
_ITEM_INDEX: Dict[int, "tuple[str, str]"] = {}


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
    _PENDING[pending_id] = {
        "items": {str(i): item for i, item in enumerate(items)},
        "created_at": time.time(),
    }
    return pending_id


def _cleanup_pending() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING.items() if now - v["created_at"] > _PENDING_TTL_SECONDS]
    for k in expired:
        _PENDING.pop(k, None)
    stale = [mid for mid, (pid, _) in _ITEM_INDEX.items() if pid not in _PENDING]
    for mid in stale:
        _ITEM_INDEX.pop(mid, None)


def _build_audit_data(product: ProductData, prices: PriceResult, markup: MarkupSpec) -> Dict[str, Any]:
    return {
        "model_code": product.model_code,
        "sizes": product.sizes,
        "pack_quantity_raw": product.pack_quantity_raw,
        "total_pieces": prices.total_pieces,
        "dozens_count": str(prices.dozens_count),
        "supplier_dozen_price": product.dozen_price,
        "supplier_single_price": product.single_price,
        "markup_kind": markup.kind.value,
        "markup_value": str(markup.value),
        "new_single_price": prices.new_single_price,
        "new_dozen_price": prices.new_dozen_price,
        "carton_total": prices.carton_total,
        "mismatch": prices.mismatch,
        "mismatch_detail": prices.mismatch_detail,
    }


def _caption_with_flags(base_caption: str, prices: PriceResult, fully_patched: bool) -> str:
    caption = base_caption
    if not fully_patched:
        caption += "\n\n⚠️ لم يتم تحديد موقع أحد السعرين بدقة في الصورة، تحقق يدوياً قبل النشر."
    if prices.mismatch:
        caption += (
            f"\n\n🚫 تعارض بالأسعار (لن يُنشر تلقائياً): {prices.mismatch_detail}\n"
            f"للتصحيح: رُد على هذه الصورة بـ \"مفرد <القيمة>\" أو \"درزن <القيمة>\"."
        )
    return caption


async def _send_processed_photos(bot, chat_id, items: List[Dict[str, Any]]) -> List[Message]:
    """Send processed photos, chunked to Telegram's 10-per-album limit.
    sendMediaGroup requires >=2 items, so a lone photo (or a final leftover
    chunk of 1) falls back to a plain send_photo. Returns the sent Message
    objects in the same order as `items`, so callers can map message_id
    back to the item that produced it.
    """
    sent: List[Message] = []
    for start in range(0, len(items), 10):
        chunk = items[start : start + 10]
        if len(chunk) == 1:
            item = chunk[0]
            msg = await bot.send_photo(chat_id, photo=item["image"], caption=item["caption"])
            sent.append(msg)
        else:
            media = [InputMediaPhoto(media=it["image"], caption=it["caption"]) for it in chunk]
            msgs = await bot.send_media_group(chat_id, media)
            sent.extend(msgs)
    return sent


def _publish_keyboard(pending_id: str, publishable_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✅ نشر الكل للقناة ({publishable_count})", callback_data=f"publish:{pending_id}"),
                InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel:{pending_id}"),
            ]
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلاً 👋\n"
        "أرسل صورة منتج واحدة أو ألبوماً كاملاً (حتى 10 صور دفعة واحدة) مع كتابة "
        "قيمة الزيادة في الكابشن -- تُضاف على مجموع سعر الكارتونة لكل منتج ثم "
        "يُقسَّم الناتج على عدد القطع لاستخراج سعر المفرد الجديد. مثال:\n"
        "+10000  أو  15%\n\n"
        "التعبئة تُقرأ تلقائياً سواء كُتبت كعدد قطع (24، 18) أو كعدد درزنات "
        "عشري (1.5 = 18 قطعة، 1.25 = 15 قطعة).\n\n"
        "لو طلعت قراءة غلط لأي منتج، رُد على صورته مباشرة بالتصحيح بدون إعادة "
        "إرسالها، مثال: مفرد 4700 -- الحقول المدعومة: مفرد / درزن / كود / قياس / تعبئة."
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


async def _process_one(index: int, message: Message, markup: MarkupSpec) -> Dict[str, Any]:
    try:
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())

        product = await asyncio.to_thread(extract_product_data, image_bytes)
        prices = compute_prices(product, markup)
        processed_image, fully_patched = await asyncio.to_thread(
            process_image, image_bytes, product, prices
        )
        caption_text = _caption_with_flags(build_caption(product, prices), prices, fully_patched)

        return {
            "ok": True,
            "index": index,
            "image": processed_image,
            "image_bytes": image_bytes,
            "caption": caption_text,
            "needs_review": prices.mismatch,
            "product": product,
            "markup": markup,
            "audit": _build_audit_data(product, prices, markup),
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

    items = [
        {
            "image": r["image"],
            "caption": r["caption"],
            "image_bytes": r["image_bytes"],
            "needs_review": r["needs_review"],
            "product": r["product"],
            "markup": r["markup"],
            "audit": r["audit"],
        }
        for r in successes
    ]
    pending_id = _remember_batch(items)

    sent_messages = await _send_processed_photos(context.bot, chat_id, items)
    for item_id, msg in zip(_PENDING[pending_id]["items"].keys(), sent_messages):
        _ITEM_INDEX[msg.message_id] = (pending_id, item_id)

    review_count = sum(1 for it in items if it["needs_review"])
    publishable_count = len(items) - review_count

    summary_lines = [f"✅ تمت معالجة {len(successes)} من {len(messages)} صورة بنجاح."]
    if review_count:
        summary_lines.append(f"🚫 {review_count} منها يحتاج مراجعة (تعارض أسعار) قبل أن يُنشر.")
    if failures:
        summary_lines.append("")
        summary_lines.append("⚠️ تعذّرت معالجة:")
        summary_lines.extend(f"  صورة {r['index']}: {r['error']}" for r in failures)

    keyboard = _publish_keyboard(pending_id, publishable_count) if publishable_count else None
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

    items_by_id: Dict[str, Dict[str, Any]] = pending["items"]

    if action == "cancel":
        _PENDING.pop(pending_id, None)
        await query.edit_message_text("❌ تم إلغاء هذه الدفعة.")
        return

    if action != "publish":
        return

    publishable_ids = [iid for iid, it in items_by_id.items() if not it["needs_review"]]
    skipped_ids = [iid for iid, it in items_by_id.items() if it["needs_review"]]

    if not publishable_ids:
        await query.answer("كل عناصر هذه الدفعة تحتاج مراجعة أولاً.", show_alert=True)
        return

    publishable_items = [items_by_id[iid] for iid in publishable_ids]
    await _send_processed_photos(context.bot, config.CHANNEL_ID, publishable_items)

    now_iso = datetime.now(timezone.utc).isoformat()
    for item in publishable_items:
        audit_log.record(
            {
                **item["audit"],
                "timestamp": now_iso,
                "approved_by_user_id": user.id,
                "approved_by_username": user.username or "",
                "channel_id": config.CHANNEL_ID,
            }
        )

    for iid in publishable_ids:
        items_by_id.pop(iid, None)

    if skipped_ids:
        await query.edit_message_text(
            f"✅ تم نشر {len(publishable_ids)} منتج.\n"
            f"⏸️ تبقّى {len(skipped_ids)} منتج يحتاج تصحيح (تعارض أسعار) قبل النشر.",
            reply_markup=_publish_keyboard(pending_id, 0),
        )
    else:
        _PENDING.pop(pending_id, None)
        await query.edit_message_text(f"✅ تم نشر {len(publishable_ids)} منتج في القناة.")


async def handle_correction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to a specific preview photo with e.g. "مفرد 4700" to fix a
    misread field without resending the photo. Recomputes from the
    ORIGINAL image bytes (not the already-patched one) so the price patch
    is always drawn fresh, never stacked on a previous edit.
    """
    message = update.effective_message
    user = update.effective_user

    if user is None or not _is_authorized(user.id):
        return

    replied = message.reply_to_message
    if replied is None:
        return

    lookup = _ITEM_INDEX.get(replied.message_id)
    if lookup is None:
        return  # not a reply to one of our previews -- ignore silently

    pending_id, item_id = lookup
    pending = _PENDING.get(pending_id)
    if pending is None or item_id not in pending["items"]:
        await message.reply_text("⚠️ انتهت صلاحية هذا العنصر أو تم نشره مسبقاً.")
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await message.reply_text('صيغة غير مفهومة. مثال: "مفرد 4700" -- الحقول: مفرد / درزن / كود / قياس / تعبئة')
        return

    field_key, value_raw = parts
    field_name = _CORRECTION_FIELD_MAP.get(field_key.strip())
    if field_name is None:
        await message.reply_text("حقل غير معروف. استخدم: مفرد / درزن / كود / قياس / تعبئة")
        return

    item = pending["items"][item_id]
    product: ProductData = item["product"]
    markup: MarkupSpec = item["markup"]

    try:
        if field_name in ("single_price", "dozen_price"):
            clean_value = int(value_raw.replace(",", "").replace("،", "").strip())
            corrected_product = product.model_copy(update={field_name: clean_value})
        else:
            corrected_product = product.model_copy(update={field_name: value_raw.strip()})

        prices = compute_prices(corrected_product, markup)
        processed_image, fully_patched = await asyncio.to_thread(
            process_image, item["image_bytes"], corrected_product, prices
        )
        caption_text = _caption_with_flags(build_caption(corrected_product, prices), prices, fully_patched)
    except (PackQuantityError, ValueError) as exc:
        await message.reply_text(f"❌ تعذر تطبيق التصحيح: {exc}")
        return
    except Exception:
        logger.exception("Correction failed for item %s/%s", pending_id, item_id)
        await message.reply_text("❌ خطأ غير متوقع أثناء تطبيق التصحيح.")
        return

    item.update(
        {
            "image": processed_image,
            "caption": caption_text,
            "needs_review": prices.mismatch,
            "product": corrected_product,
            "audit": _build_audit_data(corrected_product, prices, markup),
        }
    )

    try:
        await context.bot.edit_message_media(
            chat_id=replied.chat_id,
            message_id=replied.message_id,
            media=InputMediaPhoto(media=processed_image, caption=caption_text),
        )
    except Exception:
        logger.exception("Failed to update preview photo after correction")

    status = "✅ تم التصحيح." if not prices.mismatch else "⚠️ التصحيح مطبّق، لكن لا يزال هناك تعارض بالأسعار."
    await message.reply_text(status)


def main() -> None:
    config.validate()

    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_correction))

    logger.info("Bot starting (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
