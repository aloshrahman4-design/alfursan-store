# Range Harvester — GOLD trading bot

Python bot that trades GOLD (XAUUSD) through MetaApi and is controlled from Telegram.
Runs 24/7 as a systemd service on an Oracle Cloud Always-Free VM.

## Files

| File | Role |
|------|------|
| `range_harvester.py` | Trading engine (range/anchor strategy), Telegram control panel, self-healing connection |
| `intel.py` | Intelligence layer: multi-timeframe TA, liquidity zones, news watcher, daily trade idea (Claude) |
| `deploy.sh` | One-command update on the server (download → compile → swap → restart) |
| `tradingbot.service` | systemd unit |
| `.env.example` | Environment variables (secrets never live in the code) |

`intel.py` never places or closes orders. All trade execution stays in the engine and is manual via the buttons.

## Telegram buttons

- 📊 status · 🥇 live price · 🟢/🔴 anchors · ⚔️ dual anchor · 🚨 close bot positions (only positions with the bot's magic numbers)
- 🧠 استشارة السوق — 15m/1h/4h trend, RSI, nearest support/resistance, liquidity zones, related markets, plus a Claude read-out
- 📰 آخر الأخبار — gold-relevant headlines + Claude impact analysis
- 🎯 صفقة اليوم — one trade idea with entry / stop loss / target / confidence (also posted automatically once a day)

Automatic alerts: new gold-relevant headlines every `NEWS_POLL_MINUTES` (Claude analysis capped by `NEWS_MAX_ANALYSES_DAY`), daily trade idea at `DAILY_TRADE_HOUR_UTC`.

## Telegram commands

| Command | Effect |
|---------|--------|
| `/start` | Control panel |
| `/setkey claude sk-ant-…` | Writes the key into `.env`, reloads it live, deletes the message |
| `/setkey news <finnhub_key>` | Optional; without it news comes from a keyless RSS source |
| `/keys` | Masked key status |
| `/update` | Downloads the latest code from GitHub, compiles it, restarts |
| `/restart` | Restart the process (systemd brings it back) |

Only the first chat that ever talked to the bot is accepted; other chats are ignored.
`/update` swaps files only after the new code compiles, so a broken push cannot take the bot down.

## Deploy (first time only — afterwards use `/update` from Telegram)

```bash
curl -fsSL https://raw.githubusercontent.com/aloshrahman4-design/alfursan-store/claude/range-harvester-stability-fix-kfz55i/trading-bot/deploy.sh | bash
```

## Keys

Preferred: send `/setkey claude sk-ant-…` to the bot. Manual alternative:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/trading_bot/.env
sudo systemctl restart tradingbot
```
