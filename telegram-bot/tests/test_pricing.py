"""Regression tests for the deterministic parts of the pipeline (no network).

The ComputePricesConsistencyTests samples are transcribed from real
supplier photos reviewed during development, covering every layout/label
convention seen so far: direct piece counts (24, 18), decimal dozens
counts (1.25, 1.5), a trailing unit suffix (18PRS), and small natural
rounding between a supplier's own dozen/single prices. Run with:

    python -m unittest discover -s tests
"""
from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gemini_extractor import ProductData  # noqa: E402
from pricing import (  # noqa: E402
    MarkupKind,
    PricingMode,
    compute_prices,
    parse_markup,
    parse_pack_quantity,
    parse_pricing_mode,
)
from image_processor import build_caption  # noqa: E402


class PackQuantityTests(unittest.TestCase):
    def test_integer_is_piece_count(self):
        pieces, dozens = parse_pack_quantity("24")
        self.assertEqual(pieces, 24)
        self.assertEqual(dozens, Decimal(2))

    def test_decimal_is_dozens_count(self):
        pieces, dozens = parse_pack_quantity("1.25")
        self.assertEqual(pieces, 15)
        self.assertEqual(dozens, Decimal("1.25"))

        pieces, dozens = parse_pack_quantity("1.5")
        self.assertEqual(pieces, 18)
        self.assertEqual(dozens, Decimal("1.5"))

    def test_trailing_unit_word_ignored(self):
        pieces, _ = parse_pack_quantity("18PRS")
        self.assertEqual(pieces, 18)

    def test_garbage_raises(self):
        with self.assertRaises(Exception):
            parse_pack_quantity("لا يوجد رقم")


class MarkupParsingTests(unittest.TestCase):
    def test_flat(self):
        m = parse_markup("+10000")
        self.assertEqual(m.kind, MarkupKind.FLAT)
        self.assertEqual(m.value, Decimal(10000))

    def test_percent(self):
        m = parse_markup("15%")
        self.assertEqual(m.kind, MarkupKind.PERCENT)
        self.assertEqual(m.value, Decimal(15))

    def test_missing_value_raises(self):
        with self.assertRaises(Exception):
            parse_markup("بدون رقم")


class ComputePricesConsistencyTests(unittest.TestCase):
    """Real supplier samples reviewed during development (2026-08-16 session)."""

    SAMPLES = [
        dict(model_code="N237", sizes="31-36", pack_quantity_raw="1.25", dozen_price=48000, single_price=4000),
        dict(model_code="N774", sizes="31-36", pack_quantity_raw="1.5", dozen_price=48000, single_price=4000),
        dict(model_code="GHG-15520", sizes="25-30", pack_quantity_raw="18", dozen_price=74200, single_price=6183),
        dict(model_code="MR15824", sizes="37_41", pack_quantity_raw="18PRS", dozen_price=88350, single_price=7363),
        dict(model_code="FRTM12934", sizes="40/45", pack_quantity_raw="24", dozen_price=56250, single_price=4687),
        dict(model_code="PJKM81556", sizes="40/45", pack_quantity_raw="24", dozen_price=32000, single_price=2660),
    ]

    def _assert_internally_consistent(self, markup_caption: str):
        markup = parse_markup(markup_caption)
        for sample in self.SAMPLES:
            product = ProductData(**sample)
            prices = compute_prices(product, markup)
            self.assertEqual(prices.new_single_price * 12, prices.new_dozen_price, sample["model_code"])
            self.assertEqual(
                prices.new_single_price * prices.total_pieces, prices.carton_total, sample["model_code"]
            )
            self.assertFalse(prices.mismatch, f"{sample['model_code']} unexpectedly flagged as mismatched")

    def test_flat_markup(self):
        self._assert_internally_consistent("+10000")

    def test_percent_markup(self):
        self._assert_internally_consistent("15%")


class MismatchDetectionTests(unittest.TestCase):
    def test_flags_a_clearly_misread_digit(self):
        # 9000*24=216000 vs 32000*2=64000 -- ~70% apart, not plausible rounding.
        product = ProductData(
            model_code="X1", sizes="40/45", pack_quantity_raw="24", dozen_price=32000, single_price=9000
        )
        prices = compute_prices(product, parse_markup("+1000"))
        self.assertTrue(prices.mismatch)
        self.assertIsNotNone(prices.mismatch_detail)

    def test_does_not_flag_normal_supplier_rounding(self):
        # Real sample: dozen 74200 vs single 6183 (6183*12=74196, ~0.005% off).
        product = ProductData(
            model_code="GHG-15520", sizes="25-30", pack_quantity_raw="18", dozen_price=74200, single_price=6183
        )
        prices = compute_prices(product, parse_markup("+1000"))
        self.assertFalse(prices.mismatch)

    def test_single_price_only_is_never_flagged(self):
        product = ProductData(model_code="Y1", sizes="40/45", pack_quantity_raw="24", single_price=4000)
        prices = compute_prices(product, parse_markup("+1000"))
        self.assertFalse(prices.mismatch)


class PricingModeTests(unittest.TestCase):
    """Admin writes "درزن" in the caption to apply the markup to the dozen
    price instead of the carton total -- and the caption shows a different
    pair of fields depending on which mode was used (see build_caption).
    Real sample: FRTM12934, dozen=56250, single=4687, pack=24.
    """

    PRODUCT = ProductData(
        model_code="FRTM12934", sizes="40/45", pack_quantity_raw="24", dozen_price=56250, single_price=4687
    )

    def test_mode_detection(self):
        self.assertEqual(parse_pricing_mode("+15000"), PricingMode.CARTON)
        self.assertEqual(parse_pricing_mode("+15000 درزن"), PricingMode.DOZEN)
        self.assertEqual(parse_pricing_mode("دزن +15000"), PricingMode.DOZEN)

    def test_carton_mode_matches_default_behavior(self):
        markup = parse_markup("+15000")
        prices = compute_prices(self.PRODUCT, markup, PricingMode.CARTON)
        self.assertEqual(prices.new_single_price, 5313)
        self.assertEqual(prices.carton_total, 127512)
        caption = build_caption(self.PRODUCT, prices)
        self.assertIn("سعر المفرد", caption)
        self.assertIn("إجمالي سعر الكارتونة", caption)
        self.assertNotIn("سعر الدرزن", caption)

    def test_dozen_mode_applies_markup_to_dozen_price(self):
        markup = parse_markup("+15000 درزن")
        prices = compute_prices(self.PRODUCT, markup, PricingMode.DOZEN)
        # 56250 + 15000 = 71250; single = round(71250/12) = 5938
        self.assertEqual(prices.new_dozen_price, 71250)
        self.assertEqual(prices.new_single_price, 5938)
        caption = build_caption(self.PRODUCT, prices)
        self.assertIn("سعر المفرد", caption)
        self.assertIn("سعر الدرزن", caption)
        self.assertNotIn("إجمالي سعر الكارتونة", caption)

    def test_dozen_mode_derives_basis_from_single_when_dozen_missing(self):
        product = ProductData(model_code="Z1", sizes="40/45", pack_quantity_raw="24", single_price=4000)
        prices = compute_prices(product, parse_markup("+1000 درزن"), PricingMode.DOZEN)
        # basis = 4000*12 = 48000; +1000 = 49000; single = round(49000/12) = 4083
        self.assertEqual(prices.new_dozen_price, 49000)
        self.assertEqual(prices.new_single_price, 4083)


if __name__ == "__main__":
    unittest.main()
