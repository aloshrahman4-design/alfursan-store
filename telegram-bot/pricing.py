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
    """
    if not caption or not caption.strip():
        raise MarkupParseError(
            "لم يتم تحديد قيمة الزيادة في نص الرسالة (caption).\n"
            "أرسل الصورة مع كتابة الزيادة، مثال: +1000 أو 15%"
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
        "تعذر فهم قيمة الزيادة من الكابشن. استخدم صيغة مثل +1000 أو 15%"
    )


def _round_int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_prices(product: ProductData, markup: MarkupSpec) -> PriceResult:
    """Apply the markup to the supplier's prices.

    FLAT markup adds the same amount per single unit, and 12x that per
    dozen (so a single unit's price stays consistent whether it's counted
    on its own or as part of a dozen).

    PERCENT markup is applied independently to the supplier's single and
    dozen prices, since a supplier's dozen price is often already a bulk
    discount rather than exactly single_price * 12.

    carton_total = new_dozen_price * (pack_quantity / 12), i.e. however
    many dozens actually make up this product's carton.
    """
    single = Decimal(product.supplier_single_price)
    dozen = Decimal(product.supplier_dozen_price)

    if markup.kind is MarkupKind.FLAT:
        new_single = single + markup.value
        new_dozen = dozen + (markup.value * DOZEN)
    else:
        factor = Decimal(1) + (markup.value / Decimal(100))
        new_single = single * factor
        new_dozen = dozen * factor

    dozens_in_carton = Decimal(product.pack_quantity) / DOZEN
    carton_total = new_dozen * dozens_in_carton

    return PriceResult(
        new_single_price=_round_int(new_single),
        new_dozen_price=_round_int(new_dozen),
        carton_total=_round_int(carton_total),
        dozens_in_carton=dozens_in_carton,
    )
