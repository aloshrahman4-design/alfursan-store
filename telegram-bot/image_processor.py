"""Step 3 of the pipeline: surgical, layout-agnostic price patching.

Real supplier images don't share one layout -- a clean white bottom strip,
a colored corner badge, text stamped directly onto the photo, RTL Arabic
labels, English "D/DOZ"/"D/PC" labels, two bare stacked numbers -- so this
module does NOT assume a fixed footer template. Instead, for each price
Gemini located (see gemini_extractor.BBox), it:

  1. samples the REAL local background color from pixels just outside the
     box (works for a flat color bar, and degrades gracefully on a
     photographic background too), instead of assuming white;
  2. patches only that box with the sampled color;
  3. writes the new number back in, auto-picking black/white text for
     contrast against whatever color it just sampled.

Everything else in the image (product photo, logo, QR code, model code,
sizes) is left untouched -- only the price digits themselves are edited.
"""
from __future__ import annotations

import io
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

import config
from gemini_extractor import BBox, ProductData
from pricing import PriceResult

Color = Tuple[int, int, int]

# Padding around Gemini's box, as a fraction of the box's own size, to
# absorb small localization error without eating into neighboring text.
_BOX_PADDING_RATIO = 0.18
_MIN_PADDING_PX = 4


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _bbox_to_pixels(box: BBox, width: int, height: int) -> Tuple[int, int, int, int]:
    """Gemini's bbox is normalized 0-1000 in [y_min, x_min, y_max, x_max] order."""
    left = int(box.x_min / 1000 * width)
    top = int(box.y_min / 1000 * height)
    right = int(box.x_max / 1000 * width)
    bottom = int(box.y_max / 1000 * height)
    return left, top, right, bottom


def _pad_box(
    left: int, top: int, right: int, bottom: int, width: int, height: int
) -> Tuple[int, int, int, int]:
    box_w = max(right - left, 1)
    box_h = max(bottom - top, 1)
    pad_x = max(int(box_w * _BOX_PADDING_RATIO), _MIN_PADDING_PX)
    pad_y = max(int(box_h * _BOX_PADDING_RATIO), _MIN_PADDING_PX)
    return (
        max(left - pad_x, 0),
        max(top - pad_y, 0),
        min(right + pad_x, width),
        min(bottom + pad_y, height),
    )


def _sample_background_color(image: Image.Image, box: Tuple[int, int, int, int]) -> Color:
    """Average the ring of pixels just outside the box to estimate the real
    local background color (flat color bar, photo backdrop, whatever it is).
    """
    left, top, right, bottom = box
    width, height = image.size
    ring = max(3, int((right - left) * 0.15))

    outer = (
        max(left - ring, 0),
        max(top - ring, 0),
        min(right + ring, width),
        min(bottom + ring, height),
    )

    sample_pixels = []
    region = image.crop(outer).convert("RGB")
    region_w, region_h = region.size
    inner_left = left - outer[0]
    inner_top = top - outer[1]
    inner_right = inner_left + (right - left)
    inner_bottom = inner_top + (bottom - top)

    pixels = region.load()
    for y in range(0, region_h, max(1, region_h // 24)):
        for x in range(0, region_w, max(1, region_w // 24)):
            if inner_left <= x <= inner_right and inner_top <= y <= inner_bottom:
                continue  # skip the box interior itself (the old digits)
            sample_pixels.append(pixels[x, y])

    if not sample_pixels:
        return (255, 255, 255)

    r = sum(p[0] for p in sample_pixels) // len(sample_pixels)
    g = sum(p[1] for p in sample_pixels) // len(sample_pixels)
    b = sum(p[2] for p in sample_pixels) // len(sample_pixels)
    return (r, g, b)


def _contrast_text_color(background: Color) -> Color:
    r, g, b = background
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return (20, 20, 20) if luminance > 0.55 else (255, 255, 255)


def _fit_font(text: str, box_w: int, box_h: int, font_path: str) -> ImageFont.FreeTypeFont:
    """Largest font size (bounded by the box height) whose text still fits the box width."""
    size = max(10, int(box_h * 0.85))
    font = _font(font_path, size)
    while size > 8:
        bbox = font.getbbox(text)
        text_w = bbox[2] - bbox[0]
        if text_w <= box_w * 0.95:
            break
        size -= 1
        font = _font(font_path, size)
    return font


def _patch_price(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    box: Optional[BBox],
    new_value: int,
) -> bool:
    """Redact and rewrite a single price. Returns True if it was patched."""
    if box is None:
        return False

    width, height = image.size
    raw_box = _bbox_to_pixels(box, width, height)
    padded = _pad_box(*raw_box, width, height)
    left, top, right, bottom = padded
    if right <= left or bottom <= top:
        return False

    bg_color = _sample_background_color(image, padded)
    text_color = _contrast_text_color(bg_color)

    draw.rectangle([(left, top), (right, bottom)], fill=bg_color)

    text = f"{new_value:,}"
    font = _fit_font(text, right - left, bottom - top, config.FONT_BOLD_PATH)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    draw.text((cx, cy), text, font=font, fill=text_color, anchor="mm")
    return True


def build_caption(product: ProductData, prices: PriceResult) -> str:
    dozens_str = f"{prices.dozens_count:.2f}".rstrip("0").rstrip(".")
    return (
        f"🏷️ كود الموديل: {product.model_code}\n"
        f"📏 القياسات: {product.sizes}\n"
        f"📦 التعبئة: {prices.total_pieces} قطعة ({dozens_str} درزن)\n\n"
        f"💵 سعر المفرد: {prices.new_single_price:,} دينار\n"
        f"💰 سعر الدرزن: {prices.new_dozen_price:,} دينار\n"
        f"📦 إجمالي سعر الكارتونة: {prices.carton_total:,} دينار"
    )


def process_image(image_bytes: bytes, product: ProductData, prices: PriceResult) -> Tuple[bytes, bool]:
    """Patch the price digits in place. Returns (jpeg_bytes, fully_patched).

    fully_patched is False when at least one price had no usable bounding
    box (Gemini couldn't localize it) -- the image is still returned as-is
    in that case (old digits untouched), and the caller should tell the
    admin the caption has the correct number even though the picture might
    not, so they can crop/relabel manually before publishing if needed.

    CPU-bound (Pillow) -- callers on an asyncio event loop should run this
    via `asyncio.to_thread`.
    """
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image)

    dozen_patched = _patch_price(draw, image, product.dozen_price_bbox, prices.new_dozen_price)
    single_patched = _patch_price(draw, image, product.single_price_bbox, prices.new_single_price)

    wanted_dozen = product.dozen_price_bbox is not None
    wanted_single = product.single_price_bbox is not None
    fully_patched = (dozen_patched or not wanted_dozen) and (single_patched or not wanted_single)

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue(), fully_patched
