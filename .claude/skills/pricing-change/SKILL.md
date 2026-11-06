---
name: pricing-change
description: Use when changing or fixing price extraction, markup or rounding in telegram-bot (pricing.py, gemini_extractor.py, image_processor.py). Enforces the Decimal-only and regression-test rules of this project.
---

# Pricing change workflow (alfursan-store)

1. Read `telegram-bot/README.md` section on rejected approaches first, so a previously rejected fix is not retried.
2. Money math lives ONLY in `telegram-bot/pricing.py`, with `Decimal`. Never let the model compute a price.
3. Get the real sample numbers from the user (supplier text, dozen price, pack size). A fix without a real sample is a guess.
4. Add a failing regression case to `telegram-bot/tests/test_pricing.py` using those exact numbers, run it, see it fail.
5. Fix, run `cd telegram-bot && python -m pytest -q tests`, see it pass.
6. Remember the two supplier formats: dozen price vs pack-total price; carton mode rounds to 500 دينار.
7. Report which sample failed before and passes now. Do not claim "fixed" without that.
