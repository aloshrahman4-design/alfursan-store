# CLAUDE.md — متجر الفرسان (alfursan-store)

Project memory for Claude Code. Keep it short and current.

## What this is
Online women's clothing store (Iraq, prices in دينار عراقي, UI/messages in Iraqi Arabic, RTL).
Customers order via WhatsApp; no payment processing on-site.

## Layout
- `index.html` — public storefront. Reads products directly from Firestore REST
  (project `alfursan-market`), cart → `createOrder` → opens `wa.me` with the order + 4-char tracking code.
  `?page=<key>` selects which Facebook page/shop the visitor came from.
- `admin.html` — product upload/list. Posts to `addProductFromTelegram` (lives in another
  Firebase codebase, NOT in this repo).
- `order-status.html` — sales-rep page: PIN via `checkRepPin` → returns a secret used by `updateOrderStatus`.
- `functions/` — Firebase Functions v2 (Node 20, region `europe-west1`, codebase `store`).
  Every file exports one HTTP function; all are wired in `functions/index.js`.
  Secrets live in Secret Manager (`defineSecret`): `ORDER_STATUS_SECRET`, `STORE_REP_PIN`,
  `AUTOPOST_BOT_SECRET`, `FB_APP_ID`, `FB_APP_SECRET`.
- `telegram-bot/` — Python bot: supplier product photo → Gemini Vision extracts prices →
  `pricing.py` applies markup with `Decimal` → Pillow patches the new price into the image.
  See `telegram-bot/README.md` (Arabic, detailed design notes and rejected approaches).
- `mods/alfursan-mods/` — Claude Code mod (function hooks): dashboard pane (`/alfursan`, `/task`),
  role team (`/team`, `/send`), automatic model tiering (`/saver`). Check with `claude plugin validate`/`test`.

## Commands
- Bot tests: `cd telegram-bot && pip install -r requirements.txt pytest && python -m pytest -q tests`
- Functions load check: `cd functions && npm ci && GCLOUD_PROJECT=test node -e 'require("./index.js")'`
- Deploy functions: `firebase deploy --only functions:store`

## Rules / conventions
- Money math only in `telegram-bot/pricing.py`, with `Decimal` — never let the LLM compute prices.
  Add a regression case in `tests/test_pricing.py` for every pricing fix (real sample numbers).
- Compare secrets with `functions/safeEqual.js` (constant time), never `===`.
- Any value rendered into HTML goes through `esc()` (frontend) / `escHtml()` (fbOAuthCallback).
- Public functions (`createOrder`, `lookupOrderStatus`) must not leak other customers' order details.
- `createOrder.total` is client-computed — informational only; final price is confirmed on WhatsApp.
- Rate-limit by the RIGHTMOST `X-Forwarded-For` entry (the leftmost is attacker-controlled).
- User-facing text: Iraqi Arabic, feminine address (the customers are women).
- Commit messages: English, explain the *why*.
