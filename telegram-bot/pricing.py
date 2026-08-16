"""Step 2 of the pipeline: deterministic pricing math.

Deliberately plain Python + Decimal -- no LLM involved -- so multiplying
and adding a batch of prices can never "hallucinate". This is the one
module that is allowed to touch money.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Optional

from gemini_extractor import ProductData

DOZEN = Decimal(12)


class MarkupKind(str, Enum):
    FLAT = "flat"
    PERCENT = "percent"


@dataclass(frozen=True)
class MarkupSpec:
    kind: MarkupKind
    value: Decimal


@dataclass(frozen=True)
class PriceResult:
    new_single_price: int
    new_dozen_price: int
    carton_total: int
    dozens_in_carton: Decimal


class MarkupParseError(ValueError):
    pass


# A '%' is unambiguous, so it's checked first. A bare "+1000" / "-500" is
# treated as a flat amount added to every single-unit price.
_PERCENT_RE = re.compile(r"([+-]?\s?\d+(?:\.\d+)?)\s?%")
_FLAT_RE = re.compile(r"([+-]\s?\d+(?:\.\d+)?)")


def parse_markup(caption: Optional[str]) -> MarkupSpec:
    """Read the admin's markup instruction from the photo's caption.

    Accepts: "+1000", "+1,000", "-250", "15%", "+15%".
    The value is applied to the CARTON's total price (see compute_prices),
    not to the single/dozen price directly.
    """
    if not caption or not caption.strip():
        raise MarkupParseError(
            "لم يتم تحديد قيمة الزيادة في نص الرسالة (caption).\n"
            "أرسل الصورة مع كتابة الزيادة على مجموع الكارتونة، مثال: +10000 أو 15%"
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


def compute_prices(product: ProductData, markup: MarkupSpec) -> PriceResult:
    """Apply the markup to the CARTON TOTAL, then derive everything else from it.

    Single source of truth = the carton's total price, so there is exactly
    one number the markup touches and no risk of the single/dozen/carton
    figures drifting apart:

    1. supplier_carton_total = supplier_dozen_price * (pack_quantity / 12)
       (the dozen price is the wholesale unit, so the carton total is
       however many dozens actually make up this product's carton).
    2. Apply the markup to that ONE total:
         FLAT    -> new_carton_total = supplier_carton_total + markup.value
         PERCENT -> new_carton_total = supplier_carton_total * (1 + value/100)
    3. new_single_price = round(new_carton_total / pack_quantity)
    4. new_dozen_price and carton_total are then derived from the rounded
       single price (single * 12, single * pack_quantity) instead of
       rounded independently, so single*12 always equals dozen and
       single*pack_quantity always equals the displayed carton total --
       no independent-rounding drift between the three numbers.
    """
    dozens_in_carton = Decimal(product.pack_quantity) / DOZEN
    supplier_carton_total = Decimal(product.supplier_dozen_price) * dozens_in_carton

    if markup.kind is MarkupKind.FLAT:
        new_carton_total = supplier_carton_total + markup.value
    else:
        factor = Decimal(1) + (markup.value / Decimal(100))
        new_carton_total = supplier_carton_total * factor

    new_single_price = _round_int(new_carton_total / Decimal(product.pack_quantity))
    new_dozen_price = new_single_price * 12
    carton_total = new_single_price * product.pack_quantity

    return PriceResult(
        new_single_price=new_single_price,
        new_dozen_price=new_dozen_price,
        carton_total=carton_total,
        dozens_in_carton=dozens_in_carton,
    )
