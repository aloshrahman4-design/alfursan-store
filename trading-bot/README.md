# Range Harvester — GOLD trading bot

Python bot that trades GOLD (XAUUSD) and is controlled entirely from Telegram — keys,
updates, broker setup and diagnostics all happen in chat, with no SSH.

Execution runs either through MetaApi or, with `BROKER=capital`, directly against
Capital.com's free REST API. Market data, news and analysis need no paid service at
all, so everything except order placement keeps working even with no broker connected.
Runs as a systemd service on an Oracle Cloud Always-Free VM.

## Files

| File | Role |
|------|------|
| `range_harvester.py` | Trading engine (range/anchor strategy), Telegram control panel, self-healing connection |
| `intel.py` | Intelligence layer: multi-timeframe TA, liquidity zones, news watcher, daily trade idea (Claude) |
| `datafeed.py` | Free, keyless market data (Yahoo → Binance PAXG → Stooq) with broker-price calibration |
| `broker_capital.py` | Free direct execution via Capital.com's REST API — no paid bridge, real broker-side stops |
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
| `/setkey metaapi <token>` | Validates the new MetaApi token against the account, backs up `.env`, swaps it in and restarts — the old token keeps working if the new one fails |
| `/keys` | Masked key status |
| `/savings` | Hours and dollars saved by the weekend saver |
| `/update` | Downloads the latest code from GitHub, compiles it, restarts |
| `/restart` | Restart the process (systemd brings it back) |

Only the first chat that ever talked to the bot is accepted; other chats are ignored.
`/update` swaps files only after the new code compiles, so a broken push cannot take the bot down.

## Execution platform

`BROKER=metaapi` (default) keeps the paid MetaApi bridge. `BROKER=capital` executes
directly against Capital.com's free REST API instead: no bridge, no deployed-hours
bill, and orders carry a real broker-side stop loss (`STOP_LOSS_DIST`) that survives
the bot being closed. A demo account uses the same code path as a live one.

`broker_capital.py` mirrors the method names of the MetaApi RPC connection, so the
trading engine is unchanged. Capital.com has no magic numbers, so every deal the bot
opens is recorded locally and the magic is attached on read — the engine still only
ever touches its own positions. Run `python broker_capital.py` for a connection check.

Set it up from Telegram, no SSH:

```
/setkey capkey <api key>
/setkey capmail <email>
/setkey cappass <api password>
/setkey broker capital
```

## Paying MetaApi almost nothing

MetaApi bills per hour the account is **deployed**, and the account is only truly
needed to send and watch orders. `datafeed.py` supplies free prices (Yahoo Finance →
Binance PAXG → Stooq, no API key), so charts, levels, news context and the daily idea
never keep a billed account up.

The governor then deploys the account only when work exists — an armed anchor, an open
position, or a trade button — and undeploys it after `IDLE_SLEEP_MINUTES` of idleness
and over the weekend. Pressing a trade button wakes it transparently before executing.
The strategy is untouched: whenever an anchor is armed the account stays up, so no
breakout or pullback can be missed.

Because a broker's GOLD quote differs from any public feed, the bot records the gap
whenever the account *is* awake and applies the median offset to free candles, keeping
levels in the broker's own price frame. Outliers are rejected.

Measured on the shipped defaults: 168 h/week billed → ~12 h/week for an active trader,
about **$25/month → under $2/month**.

## Weekend cost saver

MetaApi bills per hour the account is *deployed*, and gold does not trade from Friday
21:00 UTC to Sunday 22:00 UTC. The bot undeploys the account for those 49 hours
(29% of the week) and redeploys before the open — it never sleeps while a bot position
is still open. Connection failures that mean "subscription/balance" or "bad token" are
detected and reported once, in plain language, instead of looping silently.

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
