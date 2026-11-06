---
name: pre-deploy
description: Use before deploying Firebase functions or publishing the storefront, or when the user says deploy/نشر.
---

# Pre-deploy checks

1. Run `scripts/check.sh` and show the output; stop on any failure.
2. Confirm every export in `functions/index.js` has a file (a missing module breaks the whole deploy).
3. Run the `store-security-review` checklist on the diff.
4. List new secrets needed (`firebase functions:secrets:set NAME`) and any changed function URLs used by the HTML pages.
5. Tell the user the exact command: `firebase deploy --only functions:store`. Never run the deploy without an explicit yes.
