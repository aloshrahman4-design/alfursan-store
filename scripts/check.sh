#!/usr/bin/env bash
# One command to verify the whole project: bot tests, functions load, JS syntax.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== telegram-bot tests"
(cd telegram-bot && python3 -m pytest -q tests)

echo "== functions: every export loads"
(cd functions && for f in *.js; do node --check "$f"; done && GCLOUD_PROJECT=check node -e '
const m = require("./index.js");
const missing = Object.entries(m).filter(([, v]) => !v);
if (missing.length) { console.error("empty exports:", missing.map(([k]) => k)); process.exit(1); }
console.log("exports:", Object.keys(m).join(", "));')

echo "== inline scripts in HTML parse"
python3 - <<'PY'
import re, subprocess, sys, tempfile, os
bad = 0
for page in ("index.html", "admin.html", "order-status.html"):
    html = open(page, encoding="utf-8").read()
    for i, body in enumerate(re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", html, re.S)):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as t:
            t.write(body)
        r = subprocess.run(["node", "--check", t.name], capture_output=True, text=True)
        os.unlink(t.name)
        if r.returncode:
            bad += 1
            print(f"{page} script #{i}: {r.stderr.strip().splitlines()[-1]}")
sys.exit(1 if bad else 0)
PY
echo "ALL CHECKS PASSED"
