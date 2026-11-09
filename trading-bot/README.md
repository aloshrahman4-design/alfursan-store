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
| `broker_paper.py` | Virtual account on real live prices — no broker, no verification, no cost |
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
| `/setkey metaapi <token>` | Validates the new MetaApi token, backs up `.env`, swaps it in and restarts — the old token keeps working if the new one fails. When the token opens exactly one trading account its ID is adopted automatically; when it opens several, the bot lists them with a ready-to-send `/setkey account …` line for each |
| `/live` | Back to automatic execution: resolves the account from the token, turns signal mode off, sets `BROKER=metaapi`, restarts |
| `/gold` | Asks the broker what gold is called on this account (`GOLD`, `XAUUSD`, `XAUUSD.r`, …), verifies it quotes a price, and stores it as `SYMBOL` |
| `/setkey account <id>` | Sets `ACCOUNT_ID` — needed after changing broker |
| `/keys` | Masked key status |
| `/savings` | Hours and dollars saved by the weekend saver |
| `/update` | Downloads the latest code from GitHub, compiles it, restarts |
| `/restart` | Restart the process (systemd brings it back) |

Settings are written to `.env`, which systemd reads as an `EnvironmentFile`. systemd keeps
the **last** assignment of a key, so a write that only replaced the first occurrence left a
stale duplicate below it silently winning — `/setkey broker metaapi` would appear to succeed
while `BROKER=paper` further down still decided the behaviour. Writes now drop the extras.

Only the first chat that ever talked to the bot is accepted; other chats are ignored.
`/update` swaps files only after the new code compiles, so a broken push cannot take the bot down.

## Execution platform

`BROKER=paper` needs nothing at all: the bot runs the same anchor strategy against real
live gold prices and keeps a virtual account in `paper_state.json`. Real execution needs
a broker and every regulated broker needs identity verification, so this is the way to
find out whether the strategy actually makes money before paying for anything. `/paper`
switches to it, `/report` gives the verdict — net P/L, win rate, best and worst trade.

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

## Handing a trade back to the user

The bot opens every position bare — magic number only, no take profit — so a TP appearing
on one of its trades can only have been set by the user in MT5. Two owners closing the same
position is how a trade gets closed at the wrong moment, so the bot reads a TP as "you have
this one": it stops harvesting that position, leaves it out of the panic-close button, and
lets that side go idle instead of re-entering at the anchor. Removing the TP hands it back.
Both transitions are announced once, and `/check` counts them.

## Harvest rule

Two gates close a position, and both must hold: profit is at or above `MIN_CASH_PROFIT`,
and it has given back `PULLBACK_PCT`% of the highest profit that trade reached. A share of
the peak rather than a fixed sum, because a trade that ran to +40$ deserves more room than
one sitting at +6$ — a fixed give-back either strangles the big move or lets the small one
round-trip. Set `PULLBACK_PCT=0` to go back to the fixed `PULLBACK_CASH`.

The floor is absolute: with a 5$ floor and a 10% give-back, a peak of 5.50$ has a give-back
line of 4.95$, which the floor refuses, so the trade is held rather than harvested below 5$.

`/lot` rescales the thresholds with the lot so the behaviour in price terms stays the same,
but it leaves alone any threshold the user set himself — writing one through `/setkey` pins
it (`PROFIT_PINNED=1`). Rescaling a deliberate number is how a chosen 5$ harvest silently
became 3.33$.

## Stop loss

A stop loss attached by the user is deliberately **not** a handover. A stop and the harvest
rule do not compete — the stop decides how much a trade may lose, the harvest decides when
a winner is taken — so the bot goes on managing a position the user has protected. Only a
take profit hands a trade over, because that one does compete.

What a stop does need is for the bot to notice when it fires. The close happens at the
broker, so without `note_external_closes` the position would simply vanish: no message, no
tally, and that side left holding an anchor it never re-enters — the bot would look alive
while doing nothing. A bot position that disappears on two consecutive reads (one empty read
can be a resync) is reported with its last seen result, and that side is cleared rather than
re-entered: re-entry after a stop-out would fire immediately, since price sits on the wrong
side of the anchor by definition. The user starts the next anchor himself.

## TradingView signals

TradingView posts alerts over plain HTTP to ports 80 and 443 only, IPv4 only, and cancels
a request that takes more than three seconds. It sends no header a receiver could
authenticate with, so three things stand in for that: the secret sits in the URL path
(`/hook/<HOOK_SECRET>`, generated on first boot and compared with `hmac.compare_digest`),
the four addresses TradingView publishes are the only accepted senders (`HOOK_ALLOW_IPS`,
empty to accept any), and the HTTP reply is written before any trading work starts.

The listener is ~60 lines of `asyncio.start_server` rather than a web framework: one route,
one method, and the bot has enough moving parts already. Binding port 80 as an
unprivileged user needs `AmbientCapabilities=CAP_NET_BIND_SERVICE` in the unit file.

`HOOK_MODE` starts at `watch`: a signal is reported and nothing is traded, so the user can
confirm alerts arrive correctly and on time before `/hook trade` arms it against a live
account. An alert naming both directions, or neither, is reported rather than guessed at —
`read_signal` reads intent from Arabic, English or JSON, and refusing is cheaper than
opening the wrong side.

## The capped grid (`grid.py`)

The shape is the martingale grid sold on social media: pending stop orders laddered above
and below price, a fixed step apart, the size doubling every couple of levels so whichever
way price runs, the later big lots pay back the earlier small ones.

The difference is the cap, and the cap is the whole design. Twenty levels from a 0.01 base,
uncapped, reaches 5.12 lots: 20.46 lots in total, $16,777 of margin, and $71,690 lost on a
round trip through the grid. Capped at 0.03 the same twenty levels are 0.54 lots, $443 of
margin and $1,234 — on a $2,672 account the first is not a strategy, it is a countdown.

Two limits sit on top, because a grid's risk is never one trade but the sum of the basket:
`GRID_TP_CASH` closes the whole basket in profit and re-arms at the new price,
`GRID_MAX_LOSS` (or `GRID_MAX_LOSS_PCT` of balance) closes it in loss and **halts**. The
halt is deliberate and does not clear itself: a grid that re-arms after a stop is a grid
that takes the account on the second attempt instead of the first. Only `/grid on` resumes.

It runs on its own magic numbers (777333/777444) so the anchor strategy and the grid never
see each other's positions — each would otherwise "harvest" trades the other depends on.
Margin is checked against `GRID_MARGIN_CEILING_PCT` before anything is placed. The paper
broker gained stop-order support specifically so this can be tested for free first.

### How the grid takes profit

The basket, never a single trade. A grid always holds a winner and a loser at the same
time - the buys gain while the sells bleed - so taking trades individually would close the
winners and let the losers accumulate, which is precisely how grid accounts die. Every grid
position's floating P/L is summed, and the whole basket is closed at once.

Once that sum passes `GRID_TP_CASH` it is not taken immediately: the basket is allowed to
run, its peak is tracked, and it is closed when it gives back `GRID_TRAIL_PCT`% of that
peak - the same rule the anchor strategy uses. A flat take closes a strong move at exactly
the target and leaves the rest behind. `GRID_TRAIL_PCT=0` restores the flat take. The
floor is independent of the trail, so a basket that peaks just above the target is held
rather than closed below it, and the give-back threshold is rounded before comparison
(5.60 - 10% is 5.039999... in binary, which an exact 5.04 would otherwise miss).

The give-back is never smaller than `GRID_TRAIL_MIN` ($1 by default). A percentage alone
is meaningless on a small basket: 10% of a $3 peak is $0.30, well inside what one tick
moves a 0.02-lot basket, so the trail would fire on the first tick down and behave exactly
like the flat take it was meant to replace. The floor only binds while the percentage is
smaller, so large baskets still trail by percentage.

The peak lives in memory, not in the state file: it belongs to one basket, dies with it,
and re-establishes itself after a restart rather than carrying a stale high into a new
cycle.

### One-way commitment

Both sides armed at once sounds like insurance and behaves like a tax. Price climbs $3,
the open buys gain it, and the sells that filled on the earlier dip give it back at the
same rate, so a move that should have paid lands as pennies. In a choppy session the grid
grinds itself down that way without ever being wrong about direction.

`GRID_ONE_WAY` (on by default) pulls the opposite side's pending orders the moment one
side has positions and the other has none. The basket becomes directional and keeps what
the move is worth. Both sides are armed again at the next re-arm, so nothing is lost
permanently — only the legs that would have fought the committed direction.

It fires only when exactly one side is open. If both sides already hold positions the
damage is done and cancelling pendings would not undo it, so nothing is touched.

### Side harvest, and handing the losers over

`GRID_SIDE_HARVEST` judges each direction on its own instead of waiting for the two to net
out. A winning side is no longer held hostage by the other one bleeding at the same rate:
the user watched a $3 move up land as a quarter of a dollar because the sells filled on the
earlier dip gave it straight back.

`GRID_RELEASE_LOSERS` then leaves the other side's open trades alone instead of closing them
with the winners, and records their ids so the bot never counts, manages or closes them
again — they survive teardown, the panic close and a restart. Normally "close the winners,
keep the losers" is how a grid account dies, because the losers pile up behind the bot's
back and its risk numbers stop describing reality. Here they leave the bot's books entirely
and become the user's, at his explicit request. Nothing accumulates where the bot cannot see
it, and the account-level exposure is his to manage.

The released list is capped at 200 ids so closed trades cannot grow it without bound.
