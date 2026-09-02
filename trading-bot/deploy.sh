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
sudo systemctl restart tradingbot
sleep 5
sudo systemctl status tradingbot --no-pager | head -6
echo "==> recent log"
journalctl -u tradingbot -n 5 --no-pager | grep -i "intel enabled\|booting\|error" || true
echo "DEPLOY_DONE"
