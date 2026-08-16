"""Step 1 of the pipeline: Gemini Vision reads the supplier's price data.

This module does exactly one job -- optical transcription and localization
of whatever the supplier already printed -- and returns it as strict JSON.
It never computes a price. All arithmetic lives in pricing.py so a result
can never depend on an LLM doing math.

Suppliers use wildly different layouts (a clean bottom strip, a colored
corner badge, text overlaid directly on the photo, English vs Arabic price
labels, two unlabeled stacked numbers, ...) so this module also asks for a
bounding box around each price's digits. image_processor.py uses that box
to patch just the numbers in place -- sampling the real local background
color instead of assuming a fixed white strip -- so the edit adapts to
whatever the source image actually looks like.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError, model_validator

import config

logger = logging.getLogger(__name__)

_client: Optional[genai.Client] = None

# Retried on transient failures (network blip, rate limit, momentary 5xx)
# so one flaky call doesn't fail an entire batch item outright.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.5

# A wholesale shoe/clothing price outside this range is almost certainly a
# misread digit (e.g. reading "24" as a price) rather than a real price.
_MIN_PLAUSIBLE_PRICE = 100
_MAX_PLAUSIBLE_PRICE = 5_000_000


class BBox(BaseModel):
    """Normalized 0-1000 bounding box, in Gemini's standard [y, x] order."""

    y_min: int = Field(ge=0, le=1000)
    x_min: int = Field(ge=0, le=1000)
    y_max: int = Field(ge=0, le=1000)
    x_max: int = Field(ge=0, le=1000)


class ProductData(BaseModel):
    model_code: str = Field(description="كود/رمز الموديل كما هو مكتوب حرفياً")
    sizes: str = Field(description="مدى القياسات كما هو مكتوب حرفياً، مثال: 40/45 أو 31-36")
    pack_quantity_raw: str = Field(
        description="القيمة المكتوبة أمام التعبئة/Packing كما هي حرفياً، بما فيها أي كسر عشري أو وحدة ملحقة (مثال: 24, 18, 1.25, 1.5, 18PRS)"
    )

    dozen_price: Optional[int] = Field(default=None, description="سعر الدرزن كرقم صحيح بدون رموز")
    dozen_price_bbox: Optional[BBox] = Field(default=None, description="صندوق إحاطة حول أرقام سعر الدرزن فقط")

    single_price: Optional[int] = Field(default=None, description="سعر القطعة/المفرد كرقم صحيح بدون رموز")
    single_price_bbox: Optional[BBox] = Field(default=None, description="صندوق إحاطة حول أرقام سعر المفرد فقط")

    @model_validator(mode="after")
    def _check_has_price(self) -> "ProductData":
        if self.dozen_price is None and self.single_price is None:
            raise ValueError("No price (dozen or single) could be read from the image.")
        return self

    @model_validator(mode="after")
    def _check_plausible(self) -> "ProductData":
        for label, price in (("dozen_price", self.dozen_price), ("single_price", self.single_price)):
            if price is not None and not (_MIN_PLAUSIBLE_PRICE <= price <= _MAX_PLAUSIBLE_PRICE):
                raise ValueError(f"{label}={price} is outside a plausible price range, likely a misread.")
        if not self.model_code.strip():
            raise ValueError("model_code is empty.")
        if not self.sizes.strip():
            raise ValueError("sizes is empty.")
        return self


class ExtractionError(RuntimeError):
    """Raised when Gemini can't confidently transcribe the supplier's data."""


EXTRACTION_PROMPT = """
أنت أداة استخراج بيانات وتحديد مواقع فقط (OCR + Localization) -- ممنوع عليك إجراء أي عملية حسابية إطلاقاً.

الصورة هي بطاقة منتج من مورد جملة (أحذية/ملابس). المعلومات قد تكون في شريط أسفل الصورة،
أو في زاوية (أعلى/أسفل)، أو مكتوبة مباشرة فوق الصورة نفسها بدون خلفية منفصلة. لا تفترض
مكاناً أو تصميماً ثابتاً -- ابحث في الصورة كاملة.

انسخ كل نص كما هو مكتوب حرفياً، دون تقريب أو تعديل أو حساب. الحقول الرقمية يجب أن تكون
أرقاماً صحيحة فقط، بدون رموز عملة أو فواصل أو مسافات أو أقواس.

1) model_code: كود/رمز الموديل كما هو مكتوب (قد يبدأ بحروف مثل PJKM, GHG, MR, N, RS, DX, FRTM ...).

2) sizes: مدى القياسات كما هو مكتوب حرفياً (مثال: 40/45، 31-36، 37_41).

3) pack_quantity_raw: القيمة المكتوبة أمام كلمة "التعبئة" أو "Packing" أو قبل/بعد "PRS"،
   انسخها حرفياً كما هي بما فيها أي كسر عشري أو وحدة ملحقة (مثال: "24"، "18"، "1.25"،
   "1.5"، "18PRS").

4) الأسعار -- قد تظهر بأشكال مختلفة جداً، ميّز بينها بهذا الترتيب من القواعد:
   a. إذا كان الرقم مُسمّى صراحة بكلمة "درزن" أو "دزن" أو "D/DOZ" أو "دستة" -> dozen_price.
   b. إذا كان الرقم مُسمّى صراحة بكلمة "مفرد" أو "قطعة" أو "D/PC" أو "القطعة" -> single_price.
   c. إذا ظهرت كلمة "سعر" وحدها (بدون تحديد "درزن") بجانب رقم، ووُجد رقم آخر في نفس
      البطاقة مُسمّى "مفرد" -> اعتبر الرقم المُسمّى "سعر" هو dozen_price.
   d. إذا ظهر رقمان بلا أي تسمية نصية إطلاقاً (مجرد رقمين، غالباً أحدهما فوق الآخر أو
      بجانب بعض) -> الرقم الأكبر = dozen_price، والرقم الأصغر = single_price (لأن سعر
      الدرزن يساوي تقريباً 12 ضعف سعر القطعة، فهذا يميّزهما رياضياً دون الحاجة لتسمية).
   e. إذا ظهر سعر واحد فقط في كامل الصورة، اعتبره dozen_price، واترك single_price فارغاً
      (null) بدل اختراع رقم غير موجود.
   f. إن تعذّرت قراءة سعر ما بثقة كافية، اترك حقله فارغاً (null) تماماً بدل التخمين.

5) لكل سعر وجدته (dozen_price و/أو single_price)، حدّد أيضاً صندوق إحاطة (bounding box)
   ضيقاً حول أرقام ذلك السعر فقط -- بدون رمز العملة، بدون الأقواس، بدون كلمة الوصف
   المجاورة له -- بمقياس 0 إلى 1000 نسبةً لأبعاد الصورة الكاملة، بالترتيب:
   y_min (الحافة العلوية)، x_min (الحافة اليسرى)، y_max (الحافة السفلية)، x_max (الحافة
   اليمنى). ضعه في dozen_price_bbox أو single_price_bbox المقابل. إن لم تستطع تحديد
   الموقع بدقة كافية، اترك حقل الـ bbox فارغاً حتى لو أعطيت الرقم نفسه في الحقل الرقمي.
"""


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def extract_product_data(image_bytes: bytes, mime_type: str = "image/jpeg") -> ProductData:
    """Send the raw image to Gemini Vision and return strictly-typed supplier data.

    Retries a couple of times on transient failures (network blip, rate
    limit, momentary 5xx) with a short backoff -- a validation failure
    (bad/implausible data) is NOT retried, since asking the same model the
    same question again won't fix a genuine misread.

    Blocking (network) call -- callers on an asyncio event loop should run
    this via `asyncio.to_thread`.
    """
    client = _get_client()

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
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
            break
        except ValidationError as exc:
            raise ExtractionError(f"Could not validate supplier data: {exc}") from exc
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                logger.warning(
                    "Gemini call failed (attempt %s/%s), retrying: %s", attempt, _MAX_ATTEMPTS, exc
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise ExtractionError(f"Gemini request failed after {_MAX_ATTEMPTS} attempts: {exc}") from exc
    else:
        raise ExtractionError(f"Gemini request failed after {_MAX_ATTEMPTS} attempts: {last_exc}")

    if response.parsed is not None:
        return response.parsed

    if not response.text:
        raise ExtractionError("Gemini returned an empty response.")

    try:
        data = json.loads(response.text)
        return ProductData(**data)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        logger.error("Gemini response was not valid ProductData JSON: %s", response.text)
        raise ExtractionError(f"Could not parse supplier data from the image: {exc}") from exc
