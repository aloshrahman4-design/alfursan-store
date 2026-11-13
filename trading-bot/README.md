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

## The official lot

`GRID_BASE_LOT` is **0.02**. It was 0.01 through the period when the grid was being proved
out, and moved up once the three ceilings — the position cap, the basket stop, and the
per-trade break-even — had each been shown working on the live account.

With `GRID_MAX_LOT` at 0.02 the ladder is flat at 0.02 rather than a doubling one. That is
intentional and is not a downgrade: the doubling only ever ran to the cap anyway, and a
flat ladder makes the margin and the worst case trivially predictable.

What it costs, at a $4,000 gold price and 1:500:

| levels | total lots | margin reserved | worst whipsaw (8 open) |
|--------|-----------|-----------------|------------------------|
| 12     | 0.24      | ~$386           | -$276                  |
| 8      | 0.16      | ~$257           | -$276                  |
| 6      | 0.12      | ~$193           | -$156                  |

Levels beyond `GRID_MAX_POSITIONS` reserve margin they can never use, so 8 levels is the
sensible maximum while the cap is 8. The whipsaw figure does not change between 12 and 8
for exactly that reason.

## Keys

### A pasted pair of commands

Telegram delivers a multi-line paste as **one message**. `/setkey gridmax 0.02` with
`/grid rearm` on the next line therefore arrived as the single value `0.02\n/grid rearm`,
and the old check (`"\n" in value -> reject`) answered `القيمة غير صالحة` — which reads
like the *lot* was refused, not the paste. The user retried with a different lot and got
the same answer, twice.

`handle_setkey` now takes the first line as the value and reports the rest instead of
swallowing the whole thing. It never executes the leftover lines: silently running a
command the user did not mean to send is worse than making them resend it.


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

### Halving a trade price has run away from

The owner's rule, and the exact mirror of the martingale: when a trade is proved wrong,
make it **smaller**. Every `HALF_DISTANCE` ($5) of adverse travel from the entry, half the
remaining volume is closed. What is left keeps its own break-even, trail and $2 floor.

The distance is measured on the price that would *close* the trade — a buy at bid, a sell
at ask — so the spread counts against it, exactly as the broker's own P/L does.

It is deliberately **not** funded out of grid profit. This is risk control, and it is
needed precisely when the account is losing; tying it to a profit balance would switch it
off at the moment it matters.

What it costs and what it buys, on one 0.02 sell:

| price travels | without halving | with halving |
|---|---|---|
| back to entry | $0 | **−$5** (the premium) |
| $15 against | −$30 | −$20 |
| $30 against | −$60 | **−$35** |

So it is insurance: about $5 in the ordinary case, roughly half the damage in the bad one.

**The broker's floor decides how often it can fire.** 0.01 is the smallest tradable
volume, so a 0.02 trade halves exactly once and then stops; 0.04 stages twice. The split
rounds *down* to the lot step, so it always closes less rather than more, and it never
leaves a remainder under one lot step.

It never touches: a winning trade, the anchor magics, a trade released to the user, or
anything already at the floor. A failed partial close records nothing, so it is simply
retried on the next tick rather than half-applied. `/setkey half 0` switches it off.

It runs inside the grid engine, not in the protection guard. The guard's invariant is that
it only ever tightens a stop in the user's favour and so is safe in every state; closing
part of a position is an action, and belongs where `/pause` can stop it.

### A pinned direction (`/grid down`, `/grid up`, `/grid both`)

The trend guard is a *reaction*. After every re-arm it starts from a clean slate and needs
price to travel `GRID_TREND_STEPS` from the **new** centre and hold there before it stands
a side down. In a market that has been falling for a day that is not enough: each cycle
still lays a buy ladder, and still feeds it, until the guard catches up again.

`/grid down` is the user's own standing judgement, and it does not expire on a re-arm:

- `arm()` places only sell levels; the buy side is never armed
- `refill_gaps()` will not add a buy either — otherwise the bias would be undone one
  level at a time, which is exactly how the old one-way rule leaked
- the margin a ladder reserves **halves**, because it occupies one side instead of two.
  That is what lets a grid arm at all when `arm()` has been refusing it for want of margin
- changing it cancels the standing ladder first. Leaving it armed would keep the side the
  user just stood down filling at the broker — the same hole the panel switch had: the bot
  stops deciding, the broker keeps executing
- it closes nothing. Open trades keep their stop, their break-even and their trail

`/grid both` restores two-sided arming. The guard keeps running underneath either setting.

#### Saying it when the market turns

A standing bias is a judgement, and markets change. Pinning "sells only" during a slide is
sound; leaving it pinned through the reversal is how a good call becomes a bad position.

`bias_watch()` compares the pin against what `trend_read()` actually measures, and when
they contradict it says so once — naming the new direction, what is pinned, that every new
trade is now on the losing side, and the one command that fixes it. It does **not** unpin
anything. That is the user's decision; going quiet about it would be the worst of both.

It warns once per turn, re-arming only after price settles back inside the band.

### The trend guard

A grid earns in a range and bleeds in a trend. Price walks one way, every level it crosses
opens a trade that is under water the moment it fills, and refill keeps laying new ones in
its path. That is what the -$101.65 and the -$214 both were: no rule misbehaved, the market
simply went somewhere a grid has no answer for.

`GRID_TREND` (on by default) gives it one. It measures the direction off **price against
the grid's own centre**: once price is `GRID_TREND_STEPS` (3) steps clear of the centre and
has held there for `GRID_TREND_HOLD` (60s), the side it is running over stands down — its
pending orders are cancelled and refill is forbidden from restoring them. The other side
stays armed and follows price.

Price above the centre stands the **sell** side down. That is the part worth saying twice,
because the previous attempt got exactly this backwards (see below).

Two guards keep it from thrashing:

- **The hold.** A spike three levels deep and straight back is the precise move a grid is
  built to earn from. Standing a side down for it would throw the trade away.
- **Hysteresis.** Once called, a trend stays called until price comes back inside
  `GRID_TREND_RELEASE` (1.5) steps. Without the gap, one candle either side of the
  threshold cancels and re-places the same ladder every minute.

It never touches an open position. Those keep their own stop, their own break-even and
their own trail; the guard only decides what the grid is allowed to open next.

Arming a new ladder clears the memory: whatever was stood down was stood down against the
old centre.

It costs no extra requests. The engine reads the price **once per tick** and hands the same
quote to the guard and to refill — a second call per tick is precisely how the grid produced
eleven reconnects in twenty-two minutes the last time something was added to the loop.

### Bounded recovery

The hedge-recovery bots are right about one thing — after a run of losses, a flat lot size
means you need several wins to climb out of one hole. Their fix is to double, which is why
`IRONWALL HEDGE V7`'s own promotional video shows a 20.48-lot order and -$1,770 floating.
Doubling has no bounded worst case; that is the whole objection, not the idea behind it.

`GRID_RECOVER` takes the idea and removes the ruin. After `GRID_RECOVER_AFTER` (3)
consecutive losing cycles the next ladder starts `GRID_RECOVER_STEP` (0.01) bigger, and it
stops dead at `GRID_RECOVER_MAX`, itself clamped to `GRID_MAX_LOT`:

    0 losses  ->  0.01
    3 losses  ->  0.02
    6 losses  ->  0.03
    9 losses  ->  0.03
    99 losses ->  0.03

Addition, not multiplication, against a hard ceiling. The worst case after a hundred losing
cycles is the same worst case as after none — which is exactly what a martingale cannot say.

One rule drives the counter, both ways: a cycle that books money clears the run, a cycle
that costs money lengthens it. A re-centre keeps the count, because the run is about cycles
and not about where the ladder happens to sit.

**It is off by default.** It is the only setting in the grid that raises risk rather than
lowering it, so it is the user's decision, not a default: `/setkey recover 1`.

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

### Watching what was handed over

Releasing a trade gives up the decision, not the watch. An unwatched position is how a $20
loss quietly becomes a $400 one, so `released_watcher` keeps reporting on everything the
bot handed over, while never touching it:

- **Turned green** — the one moment the user most needs, since it is his chance to close.
  Announced once per crossing, not repeated while it stays positive.
- **Sank another `GRID_RELEASED_STEP`** ($25) — stepped, so drift does not spam.
- **Closed** — detected, reported with the last P/L seen, and dropped from the list.
- **A standing summary** every `GRID_RELEASED_SUMMARY_MIN` (30), and `/mine` on demand.

The summary clock starts when the watcher starts rather than at zero: the bot restarts
often and a summary on every boot is noise. Event alerts still fire immediately.

The watcher is supervised independently of `GRID_ENABLED`, because a released trade outlives
the grid that released it — gating it on the grid would leave exactly the positions nobody
else is managing with nobody watching them.

### Sampling and the trail

A trailing exit is only as tight as how often it looks. The engine polls every 1.5s, but
while a basket is at or above `GRID_TP_CASH` the give-back threshold is live and every
tick matters, so it polls every 0.5s instead. The rest of the time there is nothing to
miss and the slower loop keeps the request rate down.

It still cannot beat a gap. If price jumps straight through the threshold between two
reads, the basket is taken at the first figure at or below it, which can be well under
the trail. That is inherent to a software trail on a basket — a broker-side take profit
protects one position, not the sum of nine — and it is the reason the floor exists: the
basket is never taken below `GRID_TP_CASH`, however far the give-back overshoots.

### Re-centring

`/grid center` (also `/grid reset`) closes the grid's own positions, cancels its pending
orders and clears the state, so the engine arms one fresh ladder on the current price.

Clearing the state alone was a footgun: the engine would arm a new ladder on top of the
pending orders still sitting at the broker and double every level. With no connection it
clears the state and says plainly that the old orders could not be cleaned up, rather than
pretending the slate is clean.

`reset()` deliberately preserves the released list. Those trades were handed over for good,
and wiping the list on a re-centre would quietly take them back — the bot would start
counting them again and the next teardown would close positions it had promised not to
touch.

### A take profit on every level

The basket rule waits for the whole net to clear its target, and that cost real money: two
buy levels ran into profit while the sell side was bleeding, the net never reached $5, price
reversed and both gains became losses. The win was there and the rule gave it back.

`GRID_TP_PER_TRADE` ($2 by default) attaches a take profit to each order as it is placed, so
every level closes at its own target independently of the basket. It lives at the broker, not
in the loop, which means it fires on a gap or while the bot is restarting — the two cases a
polled exit always loses. `tp_distance` converts cash to a price move for the order's volume
($2 on 0.01 lot of gold is a $2 move; on 0.05 it is $0.40). Set it to 0 for basket-only.

Attaching it at placement time is not enough on its own: every position opened before the
setting existed, or by an order from an older build, ran with no exit of its own. The user
watched a level sit at +$2.43 and give all of it back for precisely that reason.
`ensure_take_profits` reconciles the target against what is actually open each tick, so a
position missing one is given it, and nothing is touched twice. It is skipped on a broker
with no `modify_position`.

One sharp edge this created: `user_took_over` reads a take profit as "the user took this trade
over", which is true for anchor trades and false for the grid, since the grid now sets a TP on
everything it opens. Without scoping that test to the anchor magics, the entire grid would have
quietly exempted itself from the panic button.

## One position per side was a bug, not a design

The anchor engine harvested `buy_pos[-1]` and `sell_pos[-1]` — the newest trade on each
side and nothing else. With two buys open, one was watched and the other was invisible: it
could run into profit with nothing tracking its peak, and price would take the whole gain
back. That is exactly what happened to two 0.05 buys at 4129.42 and 4127.86.

Every open position is now harvested on its own merits, each trailing its own peak in
`peak_by_id`, so one trade closing says nothing about another. The entry for a position is
dropped when it closes, and stale ids are swept each tick against what is actually open.
The anchor price still comes from the newest trade, which is what the re-entry logic means
by "the anchor".

## The quota ran out, and the bot went blind

A screenshot showed two trades that had been +$4.60 and +$2.20 sitting at a loss with no
stop on either. `/check` gave the reason:

    The ws:getAccountInformation API allows 4320000 cpu credits per 6h ...
    Please wait some time and retry your request

Nothing was wrong with the protection logic. It could not read a price.

Counting the calls: `protect_engine` asked for positions and a price every second, and
`grid_engine` for positions, orders and a price roughly as often — about **150,000
metered requests per six-hour window**. Worse, the soft-guard change the day before had
added a *second* `get_positions()` per protect tick, so the fix for one blind spot helped
exhaust the quota that caused a bigger one.

MetaApi has the right answer built in. A **streaming connection** is pushed updates over
a websocket and keeps them in a local `terminal_state`: positions, orders, prices, account
information and symbol specifications are all read from memory, cost nothing, and are
fresher than a poll. Trades still go over the API, which is correct — they are rare.

`StreamingView` wraps the streaming connection so every existing call site is unchanged:
the five read methods are served from the cache, everything else passes through via
`__getattr__` (so `hasattr(conn, "modify_position")` and friends still work). RPC remains
the fallback if streaming will not synchronize, and the connection logs which mode it is
in.

Both engines now pace themselves by what a read costs: the sub-second tick a trailing exit
needs is safe against a local cache, and `TICK_SECS_METERED` (15s) applies when it is not.
`PaperBroker` declares `streaming = True` because its reads are a local file — without
that, demo mode would crawl at 15 seconds a look and a $2 grid cannot work that way.

The lesson, and it is not the first time in this file: **a per-tick API call is a budget,
not a free action.** The earlier version of it was eleven reconnects in twenty minutes
from fetching account info every tick; this one was the whole quota.

## Request load and reconnect noise

The grid asked the broker for account information on every tick to work out its loss limit
— a third of its request load, for a number that barely moves. Under that load price reads
started timing out, `last_price_ok` went stale past `CONNECTION_STALE_SECS`, and the
supervisor rebuilt the connection: eleven "connected successfully" messages in twenty-two
minutes, each one a real reconnect. The balance is now cached for `GRID_BALANCE_TTL` (30s).

The message itself is rate limited to one per `CONNECT_NOTE_QUIET_SECS` (15 minutes). A
reconnect is worth a log line, not a notification; saying it eleven times teaches the user
to scroll past everything the bot sends, including the messages that matter. Every
reconnect is still logged.

## The saver was blind to the grid

`our_positions_open` looked only for the anchor magics, so a grid holding twenty-four stop
orders and several open positions read to the cost governor as "nothing going on". After
twenty idle minutes — or at the weekend — it undeployed the MetaApi account underneath it.

Undeploying does not stop the grid. The pending orders sit at the broker and keep filling;
what goes away is the bot: the basket stop cannot fire, `note_external_closes` sees nothing,
and nobody is watching. The per-trade take profits would still close winners, which only
makes the shape worse — winners taken, losers accumulating, no stop, no eyes.

It now counts grid positions and armed grid pending orders as live work, and `work_pending`
treats an armed grid the same way. A broker error still fails safe by staying awake.

Switching the system off from the panel has the same shape on a smaller scale: it stops the
bot managing, not the broker filling. The toggle now says so and points at `/grid off`,
which actually cancels the orders.

### Holes in the ladder

Levels fill, close at their target, and leave gaps. The grid only re-arms once every
position *and* every order is gone, so price could end up stranded in an $18 dead zone —
nearest buy stop $11 above, nearest sell stop $7 below — with nothing to catch it, for as
long as the remaining positions stayed open.

`refill_gaps` puts a level back one step from price whenever the nearest one on that side
has drifted more than `GRID_GAP_FACTOR` (1.5) steps away. It adds pending orders only: open
positions are never touched, so nothing is realised to fix a gap. A side already holding `GRID_LEVELS` orders does not refuse — it recycles. The cap exists
to stop a trending market turning this into an order machine, but refusing outright left the
whole ladder stranded far above price with nothing covering the near side, which is the
dead zone all over again. The furthest level is cancelled to pay for the near one, so the
count never grows and the ladder walks toward price instead of being abandoned by it.

## Protection was arriving late, and the spread was why

The owner reported the break-even guard "sometimes late". It was, measurably. Walking a
price up a cent at a time through the real function:

| lot | BE_TRIGGER_CASH says protected from | a stop is actually placed at | bare window |
|---|---|---|---|
| 0.01 | $0.35 | $0.64 | $0.29 |
| 0.02 | $0.35 | **$1.28** | **$0.93** |
| 0.05 | $0.35 | $3.20 | $2.85 |

A 0.02 trade could reach +$1.27 and fall back to a loss with nothing holding it, while
the configured trigger claimed it was covered from +$0.35.

The cause was one line:

    gap = max(spread, BE_MIN_GAP)

The spread was standing in for the broker's minimum stop distance. It is not that: the
spread is buy-minus-sell, and says nothing about how close a stop may sit to the market.
On gold it cost $0.32 of price we did not owe. The real number is `stopsLevel`, which the
broker publishes per symbol, and which is often far smaller.

Three changes:

1. **Ask the broker.** `broker_stop_gap()` reads `stopsLevel` from the symbol
   specification once per session and scales points to price by the symbol's digits. With
   it, a 0.02 trade is protected from $0.84 instead of $1.28. When the broker does not
   answer, the old conservative spread stands — guessing low only earns a rejected modify.
2. **Stop being silent.** Skipping in silence is what made this look like lateness with
   no reason. A trade that is eligible but not yet placeable now says so once, with the
   profit it needs and why.
3. **Guard the rest in software.** Below `spread + gap` above entry, *any* stop would sit
   under break-even and be a losing stop, so no broker can cover that window.
   `soft_protect()` closes a trade there at about zero if it gives back everything it
   showed, rather than letting it turn red.

`soft_protect` is gated on `bot_active`; `apply_breakeven` is not. Tightening a stop is
safe in every state and must keep running when the system is off. Closing is an action,
and `/pause` has to mean it.

## Break-even

A trade that climbed part of the way to its target and then turned around used to hand the
whole gain back and keep going into loss. Once a position shows `BE_TRIGGER_CASH` ($1) of
profit its stop moves to entry, so from that point it either reaches the target or closes
flat — it cannot become a loss.

The stop is placed at entry plus the current spread plus `BE_LOCK_CASH` ($0.10), not at
entry itself: a buy opens at ask and closes at bid, so entry alone would exit at minus the
spread. "Flat" here means flat after costs.

Once secured the stop keeps following the peak, `BE_TRAIL_CASH` ($0.50) behind it, with
break-even as the floor. Break-even alone saved the trade and lost the gain: a position that
reached +$1.80 and turned around closed at zero and the $1.80 was gone. Trailing banks most
of what the move actually gave.

One consequence worth stating: with a trailing stop this tight, individual trades are
usually taken long before the basket's net reaches `GRID_TP_CASH`, so the basket rule now
fires rarely. That is the intended trade — many small certain gains instead of a few larger
ones that a reversal can erase — but it does mean the basket figures move slowly.

It only ever protects a gain. A losing trade is never given a stop by this, and an existing
stop is never loosened — the check refuses to move one that is already at or better than
break-even — so it cannot turn a live trade into a forced loss. That is why it is on by
default even though per-trade stop losses are not.

Both engines use it: the grid each tick, the anchor engine from the price it already holds.
`broker_paper` books a stop or target at its own price rather than wherever price has
reached by the next poll — booking at the current mid made a protected trade report the
very loss it had been protected from.

## A saved setting that looks unsaved

`/setkey gridmax 0.02` writes the value, restarts, and changes nothing visible — because
the lot only ever reaches the market through a **new ladder**, and at `GRID_MAX_POSITIONS`
open trades the engine will not lay one:

    if len(held) >= GRID_MAX_POSITIONS:
        await asyncio.sleep(5)      # and said nothing, for as long as that lasted

So the user changed the lot, saw nothing, changed it again, saw nothing again, and
reasonably concluded the setting was broken. It was not: the ladder that would have
carried it could never be laid. `/grid rearm` made it worse by signing off with
"أنصب السلّم من جديد خلال ثواني" even when room was zero.

Both now say it. The engine reports the block (rate-limited by `BLOCKED_NOTE_SECS`, 15
min) with the ladder that is waiting, when it will arm, and how to force it; `/grid rearm`
checks the room before promising anything.

The rule this keeps re-teaching: **when the bot declines to act, that is information the
user needs.** Silence reads as a bug in whatever they changed last.

## The position cap was advisory, and now is not

`GRID_MAX_POSITIONS` was checked in two places: before laying a fresh ladder, and before
refilling a gap. Neither is the path that matters. The ladder arms with `GRID_LEVELS` (12)
pending orders a side, and **every one of them could still fire after the cap was reached**
— so in the one scenario the cap exists for, a trend sweeping the ladder, it did nothing.
The test reproduces it: a price sweep opens all twelve against a cap of eight.

The engine now pulls the remaining pendings the moment there is no room, whether the limit
hit was the position count or `GRID_ADD_STOP_PCT` of the loss stop. The grid keeps what it
holds, can open nothing more, and refill walks levels back toward price once something
closes.

This also made the risk figures honest. `/grid` quoted a twelve-level round trip, which
with a real cap of eight overstated every loss number by about half. They are now computed
on `exposure_lots()` — what can actually be open at once — and the line says so.

## Ceilings — why twenty positions were open at once

The grid hit its basket stop with twenty positions open and took $101 in one go. Every
individual rule had behaved correctly; what was missing was any limit on the total.

Three holes, all of them introduced by fixing something else:

1. **No cap on open positions.** `GRID_LEVELS` capped *pending orders per side*. Nothing
   capped positions, and refilling plus recycling kept feeding a trend — each refilled
   level filled almost immediately, and the next tick refilled the gap it left behind.
   `GRID_MAX_POSITIONS` (8) is now a hard ceiling: no refill, no re-arm past it.
2. **Refill and one-way fought each other.** One-way cancelled the opposite side's pending
   orders; refill saw that side had nothing near price and put one straight back; one-way
   cancelled it again. The engine now tells refill which side was stood down, and refill
   leaves it alone while the commitment holds.
3. **No money ceiling on expansion.** A basket already most of the way to its stop went on
   adding levels. Past `GRID_ADD_STOP_PCT` (50%) of the loss limit, nothing new is opened.

The pattern behind all three: a cap on a part is not a cap on the whole. Each rule was
locally right and the sum was unbounded.

## The two rules the stop enforces

1. **Past the entry point, a trade can no longer be a loss.** Once profit clears
   `BE_TRIGGER_CASH` ($0.35 — enough to put a stop above entry once the spread is paid),
   the stop goes to entry plus costs. A reversal closes it flat, never red.
2. **$2 is a floor, not an exit.** `GRID_TP_PER_TRADE` is off: a fixed target capped every
   winner at exactly $2. Instead the stop trails `BE_TRAIL_CASH` behind the peak and, once
   the peak clears `BE_FLOOR_CASH` ($2) by the trail, never locks less than $2 again. The
   trade keeps running; it is taken only when it comes back to the level it defended.

Worked through: peak $1.00 locks $0.50 · peak $2.00 locks $1.50 · peak $2.50 locks $2.00 ·
peak $10.00 locks $9.50.

The floor arms at `BE_FLOOR_CASH + BE_TRAIL_CASH`, not at `BE_FLOOR_CASH`. Locking $2 the
instant profit touched $2 would close the trade there — which is the fixed target again,
wearing a different name.

A stop is never placed through the market: if the computed level is already past the
current bid (buy) or ask (sell), it is skipped. Without that check an aggressive trigger
closes the trade on the spot at a few cents instead of protecting it.

## One-way was backwards, and has been deleted

The rule read "the side holding open positions is the committed direction — stand the other
side down". In a grid that is inverted. Winners close on their own target or trailing stop
and disappear; **losers are what stays open**. So the side still holding positions is the
losing one, and standing down its opposite means cancelling the side that would have earned.

Seen live: three losing sells open, price $15 above them and still climbing, the buy side
cancelled, and refill forbidden from restoring it because the engine had stood it down.

It was switched off for a while, which was the wrong fix — a rule that inverts under load
is not a setting, it is a bug with a default. `GRID_ONE_WAY` and `GRID_COMMIT_LEVELS` are
gone from the code entirely, replaced by the trend guard above.

The lesson is the one that keeps recurring here: **read the signal off something that
cannot invert.** Position inventory flips meaning depending on which trades have already
closed. Price against the centre does not.

## Stopping

Three different things, and until now only two of them existed.

| | pending orders | open trades | system | survives a restart |
|---|---|---|---|---|
| panel switch | **left armed** | left open | off | yes (now) |
| `/pause` | cancelled | **left open** | off | yes |
| `/resume` | re-armed | left open | on | yes |
| `/grid off` | cancelled | grid trades **closed** | grid only | yes |
| `/stop` | cancelled | **all closed** | off | yes (now) |

`/resume` is its exact inverse, in one command: system on, grid on, any halt cleared,
restart. It was missing at first, which left the way back as "`/grid on`, wait for the
restart, then find the panel button" — two steps and a wait to undo one command. It also
lists what automatic behaviour comes back on the trades that stayed open, because that is
the part the user is agreeing to again.

`/pause` is "stop the bot but keep my trades". It cancels the grid's pending orders so
nothing new can open, switches the grid and the system off, and touches no position. The
protection guard keeps running on what is open — it only ever tightens a stop in the
user's favour, so there is no state in which leaving it on is unsafe.

The panel switch alone is not a stop: it stops the *bot* managing, not the *broker*
filling. Orders sitting at the broker still fire, and then nobody is watching them. The
switch now says so and points at `/pause`.

### ast.parse is not a syntax check

Adding `/resume` above `/pause` in the same handler put an assignment to `bot_active`
before the `global` declaration further down — a real SyntaxError. `ast.parse()` accepted
the file anyway: the rule is enforced when the symbol table is built, not when the source
is parsed. Only `compile()` catches it.

Everything verifies with `compile()` now, and `test_resume` compiles all six modules.

### The switch did not survive a restart

`bot_active` was a hardcoded `True`. So `/stop` set it to `False`, announced
"أطفيت النظام", and then called `os._exit(0)` — systemd restarted the process and the
system came back **on**. The same hole applied to the panel switch and to any `/update`.

It is read from `BOT_ACTIVE` now, and all three paths persist it.


`/stop` is the full shutdown: close every position the bot owns, cancel the grid's pending
orders, disable the grid, switch the system off, restart so nothing lingers in memory. It
says what it could not do — with no broker connection it reports that rather than implying
the account is flat.

Two holes this exposed, both in the path that was supposed to be the safe one:

- **Closing positions is not stopping.** The panic button closed trades and left the
  ladder armed, so the next tick filled it again. Anything that closes the grid's trades
  now disarms the grid.
- **A panic close took the user's own trades.** Positions handed over through `release`
  were skipped by the grid engine but not by `close_bot_positions`, so the button that is
  meant to protect him took the trades he had accepted responsibility for. They are now
  skipped there too, and counted as spared.

Softer options, in order: `/grid off` stops the grid alone; the panel's system switch stops
the bot managing (and warns that armed orders still fill); `/stop` stops everything.

## Testing on a demo account

Two ways, and they are not equivalent.

**A real MT5 demo through MetaApi** is the faithful one: the same execution path, the
broker's own ticks and spread, pending orders and stops handled by the broker exactly as
they are live. Open a demo at the broker, add it to MetaApi, then `/setkey account <id>`.
The bot switches to it with nothing else changed, and `/setkey account <live-id>` switches
back. Only one account is deployed at a time, so this does not add to the MetaApi bill —
but live trading is paused for as long as the test runs.

**The internal paper account** (`/paper`) needs no setup and costs nothing. It was reading
15-minute candle closes cached for a minute, which cannot test a $2 grid: price arrives in
$2-$5 jumps, so fills, targets and trailing stops all land on prices that never existed and
the result is a confident fiction. It now reads 1-minute bars (`PAPER_TF`) with a 15s cache
for short timeframes, falling back to coarser bars rather than failing. Good enough to
check that the logic behaves; still not tick data, so treat the P/L as indicative.

### Switching accounts

`/accounts` lists every trading account the MetaApi token opens, marks which one is
running, and gives a ready-to-send `/setkey account <id>` line for each. `/live` cannot do
this job: it prefers the account already configured, so from a demo it would simply confirm
the demo rather than offer the way back.

Switch only after `/stop`. Changing `ACCOUNT_ID` points the bot at a different account; it
does not close what is open on the one being left, and those positions keep their broker
stops but lose the bot that was managing them.

## The protection guard

`protect_engine` is its own supervised task, and that is the point. The same rule used to
run inside the two engines, so it stopped whenever either did: switch the system off from
the panel, or turn the grid off, and open trades carried on with nobody moving their stops.
It now covers every bot position — anchor and grid — regardless of what else is running,
skipping only trades handed to the user and anything not opened by this bot. It never
opens, closes or widens; it only tightens a stop in the user's favour, so there is no state
in which running it is unsafe.

`BE_MIN_GAP` exists because a cash trail converts to very different price distances by lot
size. $0.50 is $0.50 of price on 0.01 lot and $0.10 on 0.05 — tighter than the spread, so
the computed stop landed above the market and was refused, and the largest trades ended up
with no protection at all. The stop is now pulled back to a placeable distance rather than
skipped, and only skipped when even that would sit below break-even.

One honest consequence: with a $0.30 spread and a $0.25 minimum gap, a 0.01-lot trade needs
roughly $0.60 of profit before any stop fits above entry, whatever `BE_TRIGGER_CASH` says.
The trigger is a floor on intent, not a promise about the first cent.

### Re-centring without closing

`/grid rearm` cancels the pending orders and lays a fresh ladder around the current price,
leaving every open position exactly as it is — same entry, same stop, same protection, and
still counted against `GRID_MAX_POSITIONS`, so the new ladder can only fill into the room
that is actually left. Nothing is realised.

`/grid center` is the other one: it closes the grid's positions first, so the slate is
genuinely clean. Use it to abandon a basket; use `rearm` to keep the basket and move the
ladder back around price.
