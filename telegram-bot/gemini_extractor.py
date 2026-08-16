"""Step 1 of the pipeline: Gemini Vision reads the supplier's footer text.

This module does exactly one job -- optical transcription of whatever the
supplier already printed on the image -- and returns it as strict JSON.
It never computes a price. All arithmetic lives in pricing.py so a result
can never depend on an LLM doing math.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

import config

logger = logging.getLogger(__name__)

_client: Optional[genai.Client] = None


class ProductData(BaseModel):
    model_code: str = Field(description="كود/رقم موديل المنتج كما هو مكتوب بالضبط في الصورة")
    pack_quantity: int = Field(description="عدد القطع في الكرتونة/التعبئة كرقم صحيح")
    sizes: str = Field(description="مدى القياسات كما هو مكتوب حرفياً، مثال: 40/45")
    supplier_single_price: int = Field(description="سعر القطعة الواحدة من المورد، رقم صحيح بدون رموز")
    supplier_dozen_price: int = Field(description="سعر الدرزن من المورد، رقم صحيح بدون رموز")


class ExtractionError(RuntimeError):
    """Raised when Gemini can't confidently transcribe the footer strip."""


EXTRACTION_PROMPT = """
أنت أداة استخراج بيانات فقط (OCR منظم) -- ممنوع عليك إجراء أي عملية حسابية إطلاقاً.

اقرأ الشريط النصي أسفل صورة المنتج، والذي يحتوي عادة على:
كود الموديل، التعبئة (عدد القطع)، القياسات، سعر المفرد، سعر الدرزن.

القواعد:
- انقل كل قيمة كما هي مكتوبة حرفياً في الصورة، دون تقريب أو تعديل أو حساب.
- الحقول الرقمية (pack_quantity, supplier_single_price, supplier_dozen_price) يجب أن
  تُعاد كأرقام صحيحة فقط، بدون رموز عملة أو فواصل أو مسافات.
- إن كان رقم غير واضح تماماً، اقرأه بأعلى دقة ممكنة من الصورة نفسها فقط -- لا تخترع قيماً
  ولا تستنتجها من سياق خارجي.
"""


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def extract_product_data(image_bytes: bytes, mime_type: str = "image/jpeg") -> ProductData:
    """Send the raw image to Gemini Vision and return strictly-typed supplier data.

    Blocking (network) call -- callers on an asyncio event loop should run
    this via `asyncio.to_thread`.
    """
    client = _get_client()

    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ProductData,
            temperature=0,
        ),
    )

    if response.parsed is not None:
        return response.parsed

    if not response.text:
        raise ExtractionError("Gemini returned an empty response.")

    try:
        data = json.loads(response.text)
        return ProductData(**data)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error("Gemini response was not valid ProductData JSON: %s", response.text)
        raise ExtractionError("Could not parse supplier data from the image.") from exc
