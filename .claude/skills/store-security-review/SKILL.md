---
name: store-security-review
description: Use before merging or deploying changes to functions/*.js, index.html, admin.html or order-status.html, or when asked for a security check of the store.
---

# Security review checklist (alfursan-store)

Check each item against the diff and report pass/fail with file:line.

- Secrets/PINs compared with `functions/safeEqual.js`, never `===`.
- Rate limiting keyed on the RIGHTMOST `X-Forwarded-For` entry.
- Anything rendered into HTML escaped (`esc()` in pages, `escHtml()` in fbOAuthCallback); no raw `innerHTML` with data.
- Public functions (`createOrder`, `lookupOrderStatus`) return no other customer's items/totals; inputs capped.
- `createOrder.total` stays informational; nothing trusts client prices.
- No secret, token or PIN in code, logs, HTML or commit. `git diff` grep for `token|secret|pin|key`.
- New Firestore access: confirm Firestore rules still block public writes to `orders` and rep/admin collections.
- Facebook tokens are shown once in the browser and never stored.
Finish with a short list: blockers, then nice-to-fix.
