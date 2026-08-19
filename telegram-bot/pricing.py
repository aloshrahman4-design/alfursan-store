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
    used_derived_basis: bool = False    # True when dozen_price was missing and estimated from single_price*12


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


# The carton total is the headline number a customer actually sees quoted in
# bulk, so it's rounded to a clean denomination (nearest 500 دينار) instead
# of landing on an arbitrary exact-to-the-dinar figure -- explicitly
# requested: "70,000" reads as a real wholesale price, "70,317" reads as an
# unrounded computation artifact. Only the carton total gets this treatment
# (not single/dozen) -- that's the one figure the customer is quoted.
_CARTON_ROUNDING_STEP = Decimal(500)


def _round_to_step(value: Decimal, step: Decimal) -> int:
    return int((value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step)


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


def _is_pack_total_not_dozen(product: ProductData, total_pieces: int) -> bool:
    """Some suppliers print the TOTAL price for the whole pack in the same
    slot others use for a genuine per-dozen price -- e.g. a 15-piece pack
    where the "big" number equals single_price*15 (the pack total), not
    single_price*12 (a real dozen price). Checked live against real
    photos from the same template family: one product's big number was
    exactly 12x its single price (a real dozen price), another's was
    exactly `total_pieces`x its single price with total_pieces=15 (a pack
    total) -- no textual label distinguishes them in either image, so this
    is detected mathematically: whichever multiplier (12, or total_pieces)
    the ratio actually lands closest to. Only fires on a close match so it
    doesn't misfire on an unrelated pair of numbers (which _check_price_mismatch
    handles instead).

    Gemini can also mark this explicitly via product.price_is_pack_total --
    some suppliers show only ONE combined figure with no dozen/single
    breakdown at all (e.g. "24 زوج 61,5 الف"), so there's no single_price to
    compute a ratio against; that case is unambiguous from the layout alone
    and doesn't need the numeric heuristic.
    """
    if product.price_is_pack_total:
        return True
    if product.dozen_price is None or not product.single_price:
        return False
    ratio = Decimal(product.dozen_price) / Decimal(product.single_price)
    distance_to_pack_total = abs(ratio - total_pieces)
    distance_to_dozen = abs(ratio - DOZEN)
    return distance_to_pack_total < distance_to_dozen and distance_to_pack_total <= Decimal("0.5")


def _supplier_carton_total(product: ProductData, total_pieces: int, dozens_count: Decimal) -> Decimal:
    """Prefer the dozen price (the wholesale unit) when it's known, and
    fall back to single_price * total_pieces when only the single price
    was legible. Extraction already guarantees at least one is present.

    If dozen_price is actually a pack-total in disguise (see
    _is_pack_total_not_dozen), it's already the carton total on its own --
    using it directly instead of multiplying by dozens_count again, which
    would double-count.
    """
    if product.dozen_price is not None:
        if _is_pack_total_not_dozen(product, total_pieces):
            return Decimal(product.dozen_price)
        return Decimal(product.dozen_price) * dozens_count
    if product.single_price is not None:
        return Decimal(product.single_price) * total_pieces
    raise PackQuantityError("لا يوجد أي سعر (مفرد أو درزن) لهذا المنتج.")


def _supplier_dozen_basis(product: ProductData, total_pieces: int, dozens_count: Decimal) -> Decimal:
    """The supplier's dozen price to apply a DOZEN-mode markup to -- the
    printed dozen price if legible, or single_price*12 if only the single
    price was legible (or if the "dozen" field turned out to be a
    pack-total in disguise, see _is_pack_total_not_dozen -- there's no
    genuine per-dozen figure to use in that case either, so it's derived
    the same way as if dozen_price had never been read).

    Some pack-total layouts have no single_price at all (a single combined
    figure like "24 زوج 61,5 الف", see product.price_is_pack_total) -- the
    per-dozen basis is derived straight from that total instead:
    pack_total / dozens_count (== per-piece price * 12).
    """
    if product.dozen_price is not None and not _is_pack_total_not_dozen(product, total_pieces):
        return Decimal(product.dozen_price)
    if product.single_price is not None:
        return Decimal(product.single_price) * DOZEN
    if product.dozen_price is not None:
        return Decimal(product.dozen_price) / dozens_count
    raise PackQuantityError("لا يوجد أي سعر (مفرد أو درزن) لهذا المنتج.")


def _check_price_mismatch(
    product: ProductData, total_pieces: int, dozens_count: Decimal
) -> Tuple[bool, Optional[str]]:
    """When both prices were read, cross-check them against each other.

    single_price * total_pieces should roughly equal the carton total
    implied by the "dozen" field -- they're two ways of pricing the same
    carton. A big gap means Gemini almost certainly misread a digit in one
    of them, so the item gets flagged instead of silently trusting one of
    two disagreeing numbers. Doesn't raise: the caller still gets a usable
    PriceResult (computed from the preferred dozen-price basis) so a
    preview can be shown and corrected via reply, rather than blocking the
    whole item.

    Uses the same pack-total-vs-real-dozen interpretation as
    _supplier_carton_total (see _is_pack_total_not_dozen) so a supplier
    who prints the whole pack's total instead of a genuine dozen price
    isn't flagged as "mismatched" for a difference that was never real.
    """
    if product.dozen_price is None or product.single_price is None:
        return False, None

    from_single = Decimal(product.single_price) * total_pieces
    if _is_pack_total_not_dozen(product, total_pieces):
        from_dozen = Decimal(product.dozen_price)
    else:
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

    PricingMode.CARTON (default):
      1. supplier_carton_total = dozen_price * dozens_count (or, if only a
         single price was legible, single_price * total_pieces).
      2. new_carton_total = supplier_carton_total (+ markup, flat or %),
         then rounded to the nearest _CARTON_ROUNDING_STEP (500 دينار) --
         that's the number actually quoted to a customer in bulk, so it's
         a clean denomination rather than an arbitrary exact-to-the-dinar
         figure.
      3. new_single_price = round(carton_total / total_pieces).
      4. new_dozen_price is derived from that single price (single*12), so
         single*12 always equals dozen. single*total_pieces is NOT forced
         to equal carton_total exactly anymore -- it can drift by up to
         total_pieces/2 دينار from the rounding in step 2, which is not
         meaningful at these price levels.

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
    pack_total_not_dozen = _is_pack_total_not_dozen(product, total_pieces)

    if mode is PricingMode.DOZEN:
        # A pack-total masquerading as "dozen_price" isn't a real per-dozen
        # figure to mark up -- _supplier_dozen_basis already falls back to
        # single_price*12 in that case, same as if dozen_price were missing.
        used_derived_basis = product.dozen_price is None or pack_total_not_dozen
        new_dozen_price = _round_int(
            _apply_markup(_supplier_dozen_basis(product, total_pieces, dozens_count), markup)
        )
        new_single_price = _round_int(Decimal(new_dozen_price) / DOZEN)
        carton_total = new_single_price * total_pieces
    else:
        # CARTON mode uses the pack-total correctly as-is (see
        # _supplier_carton_total), so that's not a derived/estimated figure.
        used_derived_basis = product.dozen_price is None
        supplier_carton_total = _supplier_carton_total(product, total_pieces, dozens_count)
        new_carton_total = _apply_markup(supplier_carton_total, markup)
        # Rounded to a clean denomination -- this is the one number the
        # customer is actually quoted in bulk, see _CARTON_ROUNDING_STEP.
        # single/dozen are then derived from THIS rounded figure (not the
        # raw one), so the patched photo and the caption both agree with
        # the rounded total; single*total_pieces can drift from it by at
        # most total_pieces/2 دينار (a single extra rounding step), which
        # is invisible at these price levels.
        carton_total = _round_to_step(new_carton_total, _CARTON_ROUNDING_STEP)
        new_single_price = _round_int(Decimal(carton_total) / Decimal(total_pieces))
        new_dozen_price = new_single_price * 12

    return PriceResult(
        total_pieces=total_pieces,
        dozens_count=dozens_count,
        new_single_price=new_single_price,
        new_dozen_price=new_dozen_price,
        carton_total=carton_total,
        mode=mode,
        mismatch=mismatch,
        mismatch_detail=mismatch_detail,
        used_derived_basis=used_derived_basis,
    )
