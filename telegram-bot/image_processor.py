"""Step 3 of the pipeline: footer-strip editing only.

Hard rule from the spec: never regenerate the product photo, never touch
its colors, never apply filters. The only allowed edit is covering the
supplier's footer strip with a white block and writing the new numbers
in its place -- everything above the strip is copied through byte-for-byte
(re-encoded, not re-rendered).
"""
from __future__ import annotations

import io

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

import config
from gemini_extractor import ProductData
from pricing import PriceResult

WHITE = (255, 255, 255)
BLACK = (17, 17, 17)
RED = (196, 30, 30)


def _shape(text: str) -> str:
    """PIL has no Arabic shaping/RTL support on its own: reshape letters
    into their joined forms, then reorder for visual (display) order.
    """
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _draw_rtl_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    right_x: int,
    y: int,
    font: ImageFont.FreeTypeFont,
    fill,
) -> None:
    # anchor="ra": right-aligned horizontally, ascender-aligned vertically.
    draw.text((right_x, y), _shape(text), font=font, fill=fill, anchor="ra")


def build_caption(product: ProductData, prices: PriceResult) -> str:
    dozens_str = f"{prices.dozens_in_carton:.2f}".rstrip("0").rstrip(".")
    return (
        f"🏷️ كود الموديل: {product.model_code}\n"
        f"📏 القياسات: {product.sizes}\n"
        f"📦 التعبئة: {product.pack_quantity} قطعة ({dozens_str} درزن)\n\n"
        f"💵 سعر المفرد: {prices.new_single_price:,} دينار\n"
        f"💰 سعر الدرزن: {prices.new_dozen_price:,} دينار\n"
        f"📦 إجمالي سعر الكارتونة: {prices.carton_total:,} دينار"
    )


def process_image(image_bytes: bytes, product: ProductData, prices: PriceResult) -> bytes:
    """Return a new JPEG with the footer strip replaced by the priced data.

    CPU-bound (Pillow) -- callers on an asyncio event loop should run this
    via `asyncio.to_thread`.
    """
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = image.size

    footer_height = int(height * config.FOOTER_HEIGHT_RATIO)
    footer_top = height - footer_height

    draw = ImageDraw.Draw(image)
    draw.rectangle([(0, footer_top), (width, height)], fill=WHITE)

    margin = int(width * 0.05)
    right_x = width - margin
    title_size = max(18, int(footer_height * 0.16))
    body_size = max(16, int(footer_height * 0.13))

    bold_font = _font(config.FONT_BOLD_PATH, title_size)
    body_font = _font(config.FONT_BOLD_PATH, body_size)

    y = footer_top + int(footer_height * 0.08)
    line_gap = int(footer_height * 0.16)

    _draw_rtl_line(draw, f"كود الموديل: {product.model_code}", right_x, y, bold_font, BLACK)
    y += line_gap
    _draw_rtl_line(draw, f"القياسات: {product.sizes}", right_x, y, body_font, BLACK)
    y += line_gap
    _draw_rtl_line(draw, f"سعر المفرد: {prices.new_single_price:,} د.ع", right_x, y, body_font, RED)
    y += line_gap
    _draw_rtl_line(draw, f"سعر الدرزن: {prices.new_dozen_price:,} د.ع", right_x, y, body_font, RED)
    y += line_gap
    _draw_rtl_line(draw, f"إجمالي الكارتونة: {prices.carton_total:,} د.ع", right_x, y, bold_font, RED)

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()
