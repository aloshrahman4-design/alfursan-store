#!/usr/bin/env bash
# One-command deploy for the Range Harvester bot on the Oracle VPS.
#   curl -fsSL https://raw.githubusercontent.com/aloshrahman4-design/alfursan-store/claude/range-harvester-stability-fix-kfz55i/trading-bot/deploy.sh | bash
# Safe: downloads to temp files, compiles, and only then swaps them in. Never touches .env.
set -euo pipefail

BRANCH="${BRANCH:-claude/range-harvester-stability-fix-kfz55i}"
RAW="https://raw.githubusercontent.com/aloshrahman4-design/alfursan-store/${BRANCH}/trading-bot"
DIR="${HOME}/trading_bot"
cd "$DIR"
# prefer IPv4 when the host is configured for it: unroutable IPv6 makes curl hang
grep -qs "^FORCE_IPV4=1" "$DIR/.env" && CURL4="-4" || CURL4=""

# One list, used for downloading, compiling and swapping. It was written out three
# times by hand, and grid.py - the largest module in the project, and the one that
# holds every protection - was missing from all three. This script would then report
# a successful deploy while leaving the old grid.py in place. The bot's own /update
# reads this set from CODE_FILES, which is why that path was correct and this was not.
FILES=(range_harvester.py intel.py datafeed.py broker_capital.py broker_paper.py grid.py)

echo "==> downloading from branch: $BRANCH"
for f in "${FILES[@]}"; do
  curl -fsSL ${CURL4:-} --connect-timeout 15 --retry 2 "$RAW/$f" -o "$f.new"
done
curl -fsSL ${CURL4:-} --connect-timeout 15 --retry 2 "$RAW/requirements.txt" -o requirements.txt

echo "==> installing/updating python packages"
./venv/bin/pip install -q -r requirements.txt

echo "==> compile check"
# Compile them TOGETHER and swap only if every one of them is good: a half-swapped
# set is a bot that imports a new range_harvester against an old grid.
./venv/bin/python -m py_compile "${FILES[@]/%/.new}"
for f in "${FILES[@]}"; do mv "$f.new" "$f"; done
echo "COMPILE_OK"

echo "==> version now on disk"
./venv/bin/python - <<'EOF'
import hashlib, os
F = ("range_harvester.py", "intel.py", "datafeed.py",
     "broker_capital.py", "broker_paper.py", "grid.py")
h = hashlib.sha256()
for n in sorted(F):
    h.update(n.encode())
    h.update(open(n, "rb").read() if os.path.exists(n) else b"")
print("FINGERPRINT:", h.hexdigest()[:7])
EOF

echo "==> restarting service"
# Passwordless sudo is the normal path; if it is not available, killing the process is
# enough because the unit has Restart=always, so systemd brings it straight back.
if sudo -n systemctl restart tradingbot 2>/dev/null; then
  echo "restarted via systemctl"
else
  pkill -f range_harvester.py || true
  echo "restarted by exiting the process (systemd Restart=always)"
fi
sleep 6
systemctl status tradingbot --no-pager 2>/dev/null | head -6 || pgrep -af range_harvester.py || true
echo "==> recent log"
journalctl -u tradingbot -n 8 --no-pager 2>/dev/null | grep -i "intel enabled\|booting\|error" || true
echo "DEPLOY_DONE"
