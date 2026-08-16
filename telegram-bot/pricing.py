"""Step 2 of the pipeline: deterministic pricing math.

Deliberately plain Python + Decimal -- no LLM involved -- so multiplying
and adding a batch of prices can never "hallucinate". This is the one
module that is allowed to touch money.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Optional, Tuple

from gemini_extractor import ProductData

DOZEN = Decimal(12)

# When a supplier prints BOTH prices and they don't agree mathematically
# within this tolerance, it's a strong signal Gemini misread a digit --
# see _check_price_mismatch(). 5% comfortably covers normal supplier
# rounding (checked against real samples: worst case ~0.25% off) while
# still catching a genuinely wrong digit (which is off by tens of percent).
_MISMATCH_TOLERANCE = Decimal("0.05")


class MarkupKind(str, Enum):
    FLAT = "flat"
    PERCENT = "percent"


class PricingMode(str, Enum):
    """Which figure the admin's markup is applied to -- and, symmetrically,
    which aggregate the caption/image show instead of the OTHER one. Chosen
    per photo via a caption keyword (see parse_pricing_mode), not a fixed
    setting, since different suppliers/deals get quoted either way.
    """

    CARTON = "carton"  # markup on the whole carton; caption shows مفرد + إجمالي الكرتونة (no درزن)
    DOZEN = "dozen"    # markup on the dozen price; caption shows مفرد + درزن (no إجمالي)


_DOZEN_MODE_KEYWORDS = ("درزن", "دزن")


def parse_pricing_mode(caption: Optional[str]) -> PricingMode:
    """CARTON is the default (unchanged behavior for a bare "+10000" or
    "15%") -- add the word "درزن" anywhere in the caption to switch to
    DOZEN mode instead, e.g. "+15000 درزن".
    """
    if caption and any(kw in caption for kw in _DOZEN_MODE_KEYWORDS):
        return PricingMode.DOZEN
    return PricingMode.CARTON


@dataclass(frozen=True)
class MarkupSpec:
    kind: MarkupKind
    value: Decimal


@dataclass(frozen=True)
class PriceResult:
    total_pieces: int
    dozens_count: Decimal          # e.g. 1.5, 1.25, 2 -- for display "(X درزن)"
    new_single_price: int
    new_dozen_price: int
    carton_total: int
    mode: PricingMode = PricingMode.CARTON
    mismatch: bool = False              # True when the supplier's own dozen/single prices don't agree
    mismatch_detail: Optional[str] = None


class MarkupParseError(ValueError):
    pass


class PackQuantityError(ValueError):
    pass


# A '%' is unambiguous, so it's checked first. A bare "+1000" / "-500" is
# treated as a flat amount added to the carton total.
_PERCENT_RE = re.compile(r"([+-]?\s?\d+(?:\.\d+)?)\s?%")
_FLAT_RE = re.compile(r"([+-]\s?\d+(?:\.\d+)?)")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_markup(caption: Optional[str]) -> MarkupSpec:
    """Read the admin's markup instruction from the photo's caption.

    Accepts: "+1000", "+1,000", "-250", "15%", "+15%". Applied to the
    carton total or the dozen price depending on the pricing mode -- see
    parse_pricing_mode() and compute_prices().
    """
    if not caption or not caption.strip():
        raise MarkupParseError(
            "لم يتم تحديد قيمة الزيادة في نص الرسالة (caption).\n"
            "أرسل الصورة (أو الألبوم) مع كتابة الزيادة على مجموع الكارتونة، مثال: +10000 أو 15%"
        )

    cleaned = caption.replace(",", "").replace("،", "").replace("٬", "")

    percent_match = _PERCENT_RE.search(cleaned)
    if percent_match:
        value = Decimal(percent_match.group(1).replace(" ", ""))
        return MarkupSpec(kind=MarkupKind.PERCENT, value=value)

    flat_match = _FLAT_RE.search(cleaned)
    if flat_match:
        value = Decimal(flat_match.group(1).replace(" ", ""))
        return MarkupSpec(kind=MarkupKind.FLAT, value=value)

    raise MarkupParseError(
        "تعذر فهم قيمة الزيادة من الكابشن. استخدم صيغة مثل +10000 أو 15%"
    )


def _round_int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_pack_quantity(raw: str) -> Tuple[int, Decimal]:
    """Interpret the supplier's "تعبئة/Packing" field.

    Two conventions are used across suppliers (confirmed against real
    samples: "24", "18", "18PRS" vs. "1.25", "1.5"):
      - A whole number ("24", "18", "18PRS") is already the total PIECE
        count per carton.
      - A decimal ("1.25", "1.5") is a DOZENS count -- e.g. 1.5 dozen = 18
        pieces, 1.25 dozen = 15 pieces. Fractional piece counts don't
        physically exist, so a decimal value unambiguously means dozens.

    Returns (total_pieces, dozens_count).
    """
    match = _NUMBER_RE.search(raw or "")
    if not match:
        raise PackQuantityError(f"تعذر فهم قيمة التعبئة: {raw!r}")

    try:
        value = Decimal(match.group())
    except InvalidOperation as exc:
        raise PackQuantityError(f"تعذر فهم قيمة التعبئة: {raw!r}") from exc

    if value == value.to_integral_value():
        total_pieces = int(value)
        dozens_count = value / DOZEN
    else:
        dozens_count = value
        total_pieces = _round_int(value * DOZEN)

    if total_pieces <= 0:
        raise PackQuantityError(f"قيمة تعبئة غير صالحة: {raw!r}")

    return total_pieces, dozens_count


def _supplier_carton_total(product: ProductData, total_pieces: int, dozens_count: Decimal) -> Decimal:
    """Prefer the dozen price (the wholesale unit) when it's known, and
    fall back to single_price * total_pieces when only the single price
    was legible. Extraction already guarantees at least one is present.
    """
    if product.dozen_price is not None:
        return Decimal(product.dozen_price) * dozens_count
    if product.single_price is not None:
        return Decimal(product.single_price) * total_pieces
    raise PackQuantityError("لا يوجد أي سعر (مفرد أو درزن) لهذا المنتج.")


def _supplier_dozen_basis(product: ProductData) -> Decimal:
    """The supplier's dozen price to apply a DOZEN-mode markup to -- the
    printed dozen price if legible, or single_price*12 if only the single
    price was legible. Extraction already guarantees at least one is present.
    """
    if product.dozen_price is not None:
        return Decimal(product.dozen_price)
    if product.single_price is not None:
        return Decimal(product.single_price) * DOZEN
    raise PackQuantityError("لا يوجد أي سعر (مفرد أو درزن) لهذا المنتج.")


def _check_price_mismatch(
    product: ProductData, total_pieces: int, dozens_count: Decimal
) -> Tuple[bool, Optional[str]]:
    """When both prices were read, cross-check them against each other.

    single_price * total_pieces should roughly equal dozen_price *
    dozens_count -- they're two ways of pricing the same carton. A big gap
    means Gemini almost certainly misread a digit in one of them, so the
    item gets flagged instead of silently trusting one of two disagreeing
    numbers. Doesn't raise: the caller still gets a usable PriceResult
    (computed from the preferred dozen-price basis) so a preview can be
    shown and corrected via reply, rather than blocking the whole item.
    """
    if product.dozen_price is None or product.single_price is None:
        return False, None

    from_single = Decimal(product.single_price) * total_pieces
    from_dozen = Decimal(product.dozen_price) * dozens_count
    bigger = max(from_single, from_dozen)
    if bigger == 0:
        return False, None

    diff_ratio = abs(from_single - from_dozen) / bigger
    if diff_ratio <= _MISMATCH_TOLERANCE:
        return False, None

    detail = (
        f"مفرد {product.single_price:,}×{total_pieces} قطعة = {from_single:,.0f} دينار، "
        f"بينما درزن {product.dozen_price:,}×{dozens_count} = {from_dozen:,.0f} دينار "
        f"(فرق {diff_ratio * 100:.1f}٪)"
    )
    return True, detail


def _apply_markup(base: Decimal, markup: MarkupSpec) -> Decimal:
    if markup.kind is MarkupKind.FLAT:
        return base + markup.value
    factor = Decimal(1) + (markup.value / Decimal(100))
    return base * factor


def compute_prices(
    product: ProductData, markup: MarkupSpec, mode: PricingMode = PricingMode.CARTON
) -> PriceResult:
    """Apply the markup to ONE figure (chosen by `mode`), then derive
    everything else from it -- so there is exactly one number the markup
    touches and no risk of the single/dozen/carton figures drifting apart.

    PricingMode.CARTON (default, unchanged from the original design):
      1. supplier_carton_total = dozen_price * dozens_count (or, if only a
         single price was legible, single_price * total_pieces).
      2. new_carton_total = supplier_carton_total (+ markup, flat or %).
      3. new_single_price = round(new_carton_total / total_pieces).
      4. new_dozen_price and carton_total are derived from that ROUNDED
         single price (single*12, single*total_pieces) rather than rounded
         independently, so single*12 always equals dozen and
         single*total_pieces always equals carton_total.

    PricingMode.DOZEN (admin writes "درزن" in the caption):
      1. supplier_dozen_basis = dozen_price (or single_price*12 if only
         the single price was legible).
      2. new_dozen_price = supplier_dozen_basis (+ markup, flat or %),
         rounded.
      3. new_single_price = round(new_dozen_price / 12).
      4. carton_total is still derived (single*total_pieces) for internal
         consistency/audit even though DOZEN mode's caption doesn't show it.

    Which of {دزن, إجمالي} actually gets displayed is build_caption's job
    (see image_processor.py) -- this function always fills in all three
    numbers so the image patch can still update whichever digits are
    physically printed on the photo regardless of caption mode.
    """
    total_pieces, dozens_count = parse_pack_quantity(product.pack_quantity_raw)
    mismatch, mismatch_detail = _check_price_mismatch(product, total_pieces, dozens_count)

    if mode is PricingMode.DOZEN:
        new_dozen_price = _round_int(_apply_markup(_supplier_dozen_basis(product), markup))
        new_single_price = _round_int(Decimal(new_dozen_price) / DOZEN)
    else:
        supplier_carton_total = _supplier_carton_total(product, total_pieces, dozens_count)
        new_carton_total = _apply_markup(supplier_carton_total, markup)
        new_single_price = _round_int(new_carton_total / Decimal(total_pieces))
        new_dozen_price = new_single_price * 12

    carton_total = new_single_price * total_pieces

    return PriceResult(
        total_pieces=total_pieces,
        dozens_count=dozens_count,
        new_single_price=new_single_price,
        new_dozen_price=new_dozen_price,
        carton_total=carton_total,
        mode=mode,
        mismatch=mismatch,
        mismatch_detail=mismatch_detail,
    )
