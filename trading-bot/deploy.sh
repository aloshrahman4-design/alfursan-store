#!/usr/bin/env bash
# One-command deploy for the Range Harvester bot on the Oracle VPS.
#   curl -fsSL https://raw.githubusercontent.com/aloshrahman4-design/alfursan-store/claude/range-harvester-stability-fix-kfz55i/trading-bot/deploy.sh | bash
# Safe: downloads to temp files, compiles, and only then swaps them in. Never touches .env.
set -euo pipefail

BRANCH="${BRANCH:-claude/range-harvester-stability-fix-kfz55i}"
RAW="https://raw.githubusercontent.com/aloshrahman4-design/alfursan-store/${BRANCH}/trading-bot"
DIR="${HOME}/trading_bot"
cd "$DIR"

echo "==> downloading from branch: $BRANCH"
curl -fsSL "$RAW/range_harvester.py" -o range_harvester.py.new
curl -fsSL "$RAW/intel.py" -o intel.py.new
curl -fsSL "$RAW/requirements.txt" -o requirements.txt

echo "==> installing/updating python packages"
./venv/bin/pip install -q --upgrade -r requirements.txt

echo "==> compile check"
./venv/bin/python -m py_compile range_harvester.py.new intel.py.new
mv range_harvester.py.new range_harvester.py
mv intel.py.new intel.py
echo "COMPILE_OK"

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
