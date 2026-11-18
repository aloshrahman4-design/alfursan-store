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

## The audit, and why the restart rule is inverted

He asked how many times I had said "my mistake" today, and told me to stop fixing what
he happens to trip over and go find the rest. Fair. So the sweep is nine machine checks
over the whole codebase, hunting the *classes* of bug made that day rather than their
instances — it runs with the suite, as `test_audit`:

1. every module **compiles** — `ast.parse` accepts a `global` placed after an
   assignment, only `compile()` rejects it, and that exact bug shipped twice
2. no setting is saved into a **silent no-op**
3. no safety limit is a **percentage of another safety limit**
4. every `/command` a message names actually **exists**
5. every alias resolves, every setting is **reachable**
6. a reply to a typed command is never **rate-limited away**
7. being unable to arm never stops it **managing** open trades
8. no test file can pass **without asserting**
9. no hot loop **buys a price** it can get free

It found thirteen things on the first run. Twelve were one bug wearing twelve hats:
`/setkey` only restarted for a hand-kept list of keys, and `HOOK_*`, `BOT_ACTIVE`,
`CAPITAL_DEMO`, `META_API_TOKEN` and `SERVER_TRAIL` had all been forgotten into it — the
command answered "تم" and changed nothing. `SERVER_TRAIL` was mine from the same morning.

A list you have to remember to extend is a bug generator. So the rule is inverted:
**everything restarts unless `LIVE_KEYS` exempts it**, and a key earns that exemption
only by appearing in the body of a `refresh_keys()`. A needless restart costs two
seconds; a missed one is a setting that silently does nothing.

My first `LIVE_KEYS` had seven names. Four were an assumption I had not checked, and all
four are read once at import. The audit verifies the list against `refresh_keys()` now,
so it cannot drift back.

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

## A hard stop on the order, for the hedge side only

*"Any sell that reverses a dollar, close it — so if price rises we are not left holding
hanging sells. Only for this period, while we are targeting the sell side."*

That is not the per-trade stop he turned down earlier, and the difference is the reason.
He is already deep on one side. The hedge is meant to work quickly or be cut; what he
does not want is a second pile of stranded trades on top of the first.

`GRID_STOP_DIST` is written onto the order at placement, so the **broker** holds it: no
polling, and it fires while the API is throttled. A sell's stop sits above its entry, a
buy's below.

It is never applied to a position that is already open. Writing a $1 stop onto trades
sitting $20 under water would shut them all at once, and nothing he asked for means that.

## Give the rule to the server, stop polling for it

Everything else in this file protects a trade by **looking** at it: read the price, read
the position, decide, write a new stop. That is a loop, and a loop only works while the
API is answering — which, for most of one day, it was not. Every fix up to here reduced
how much the loop cost. None of them removed the dependency, and the owner said so.

MetaApi takes the whole rule as data and runs it itself. `trailingStopLoss` is accepted
on `CreateMarketTradeOptions`, on `PendingTradeOptions`, and on `modify_position()`, and
his rule maps onto it field for field:

| his words | the field |
|---|---|
| "مجرد ما تدخل بالربح خلي وقف عند نقطة الدخول" | threshold `0.35` → stopLoss `0.10` |
| "وبعدها تمشي معاها بالربح" | distance `0.50` behind |
| "تعدّي 2 دولار ما ينزل الوقف تحتها" | threshold `2.00` → stopLoss `2.00` |

`units: RELATIVE_CURRENCY` means those numbers are **dollars of profit**, not price. One
config is therefore correct at 0.01 lots and at 0.05 lots with no arithmetic on our side
— which is precisely the thing the hand-rolled version kept getting wrong, from
`BE_TRAIL_CASH` being tighter than the spread on a big lot to the whole `BE_MIN_GAP`
episode. And MetaApi resolves distance against threshold by taking whichever ends up
closest to price, so the trail tightens past the floor and the floor holds beneath it —
his rule exactly, with no logic of ours in the middle.

Every order is now born carrying it, and `ensure_server_trail()` hands it to any position
that is not already holding one: one write, once, per trade.

What that buys, and it is the whole point: **the protection no longer depends on this
program.** It holds when the bot is off, when the VM is down, when the quota is spent,
when the phone is in a drawer. The polling guard is still there and still useful as a
backstop, but the money no longer rides on it.

## The price was never worth paying for

`datafeed.py` has been in this repo from the start, with this at the top of it:

    free market data, so MetaApi is only needed to place orders.

It pulls gold from Yahoo, Binance PAXG and Stooq, and it keeps a running calibration
against the broker — `/check` reports the median offset, currently six cents over forty
samples. And both engines were still asking **MetaApi** for a price on every tick, at a
metered cost, for a number three public feeds give away and that the bot had already
measured against this broker.

`market_price()` takes the broker's quote while reads are free (streaming) and the public
feed otherwise, and refreshes the calibration whenever it can see both. `grid.PRICE_HOOK`
routes the grid engine and refill through the same source. `datafeed.get_price_fast()`
reads 1m bars rather than `get_price()`'s 15m closes, so the quote is about fifteen
seconds old instead of fifteen minutes.

    before today (1s tick, everything from the broker)   151,200 per 6h
    after the morning fix (15s tick)                       7,200     21x less
    after moving the price off the meter                   4,320     35x less

The part that matters more than the count: **the bot can now see a price while the broker
is throttled.** It knows where the market is, and moving a stop is a *write* — billed on
a different budget from the reads. So protection keeps working through a throttle instead
of going dark, which was the failure the owner actually suffered.

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

### Refusing the metered path at all

Retrying streaming every 15 minutes was not enough, because in between the bot kept
running on RPC — and that is what keeps the quota empty, which is what stops the
handshake from succeeding. The bot was feeding the loop it was trying to leave.

`STREAM_ONLY` (on by default) makes a failed handshake **wait** instead of falling back.
Waiting costs nothing and lets the window clear; running metered costs credits and does
not work. It is the better failure. The bot says so once, notes that open trades at the
broker are unaffected, and points at a manual stop in MT5 for anyone who wants cover
right now.

It is not stubborn about it: after `STREAM_GIVEUP_TRIES` (6, so about 90 minutes) it
runs metered rather than stay dead, and says which it is doing. `/setkey streamonly 0`
turns the waiting off for good.

### Getting back off the metered path

Streaming has to synchronize once, and that synchronization itself costs credits. So when
the quota is *already* exhausted, the streaming attempt fails and the bot lands on the RPC
fallback — where it keeps spending and never earns its way back. `ensure_connection`
therefore timestamps the fallback and retries streaming every `STREAM_RETRY_SECS` (15
min), tearing the old connection down first.

### A blind bot is a one-way valve

The user's own words: *"the bot keeps opening trades but doesn't follow them and close
them."* That is exactly right, and it is the most dangerous thing in this whole episode.

- **Opening is passive.** A pending order sits at the broker and fills by itself. It costs
  us nothing and happens whether or not the bot can see.
- **Closing is active.** Harvesting, trailing, break-even, the basket stop — every one of
  them needs to read a price and a position list, and those are precisely what a quota
  refusal blocks.

So a blind bot keeps accumulating positions and never closes one. With the basket stop
switched off (the owner had set `GRID_MAX_LOSS` to 99999 to take manual control) there is
nothing left to stop it at all.

The fix is a dead man's switch. `arm()` and `refill_gaps()` record every order id they
place into `pending_ids`; if no read has succeeded for `BLIND_SECS` (2 min),
`cancel_known()` cancels all of them **from that record**, so it needs no read to work —
and cancelling is a trade operation on its own budget, which is why it still goes through
when the read methods are refused.

It fires once, never touches an open position, and re-arms itself the moment a read
succeeds.

### Backing off when the broker says to wait

A later `/check` showed all three reads refused, not just one. The worst response to
"you have spent your quota, wait" is to keep asking at tick rate: every refused call
still counts against the window it is waiting to clear, so the bot can hold itself
outside indefinitely.

`note_api_error()` recognises a quota refusal and puts reads to sleep for
`QUOTA_COOLDOWN_SECS` (5 min); both loops check `cooling_down()` before asking and
treat a refusal as a pause rather than a crash. It applies only on the metered path —
there is nothing to ration when reads are streamed.

### /check lost everything to one throttled read

The three reads shared a single `try`, so when `getAccountInformation` alone was throttled
— MetaApi meters it separately — the price, the positions and the connection mode all
disappeared behind one error message, and the only question that mattered ("is anything
working?") went unanswered. They are read independently now, each failure is listed, a
quota error is explained in plain words, and the first line of `/check` says whether reads
are streamed or metered.

It also stopped lying. With all three reads refused it still printed "✅ الاتصال شغال"
over `None` for the balance, `None` for the price and zero positions — which sends the
user hunting for a trading problem that does not exist. When nothing could be read it now
says so, adds that the open trades are still safe at the broker, and points at the manual
stop in MT5 as the way to protect them meanwhile.

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
3. **Guard the rest in software — off by default.** Below `spread + gap` above entry,
   *any* stop would sit under break-even and be a losing stop, so no broker can cover
   that window. `soft_protect()` can close a trade there at about zero rather than let
   it turn red.

   The owner tried it and turned it down: he would rather a trade keep running than be
   shut at zero on a wobble, and that is his call to make. It ships off. A feature that
   **closes trades** does not get to be on by default because it seemed like a good idea;
   `BE_SOFT=1` switches it on for anyone who wants it. What he actually asked for is
   untouched — a stop at entry the moment the trade is in profit, then the trail, then
   the $2 floor.

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

## Unable to arm is not unable to manage

The brake above introduced a worse bug than the one it fixed. Both "cannot arm" paths —
the new drawdown brake and the older position cap — ended in `continue`, which skips the
rest of the tick. So a grid that was holding positions but was not *armed* (the state
after `/pause` then `/resume`, or right after a harvest) silently stopped harvesting
them, stopped checking the basket stop, stopped everything. The trades were dropped.

Arming is about taking on **new** exposure. Managing is about what is already there. They
must not share an exit. The engine now notifies and falls through, and only returns early
in the two cases where there is genuinely nothing to do: the ladder armed fine, or nothing
is armed and nothing is open.

Both messages now say plainly that the open trades keep their protection, their trailing
and their harvest while the grid itself stands down.

## Hedging is not expanding, and the brake had to learn the difference

The owner, after the brake had locked him out: *"I want the bot working on the way down.
If it goes up, it just holds — I have trades above, and if price comes back I'm in
profit."*

That is a coherent book, and the brake was refusing the half of it that helps. He is net
long and losing. A **sell** does not add risk to that account, it cancels some. Blocking
it pinned him to the only direction that was hurting him, which is the opposite of what
a brake is for.

`net_exposure()` signs every position on the symbol — grid, anchor and handed-over alike,
because direction no more cares which engine opened a trade than margin does.
`hedge_side()` names the side that moves the account toward flat. While braked, the
engine arms **that side only**, capped at `abs(net)` lots so it can never run past flat
and start building fresh exposure in the other direction.

Flat and braked still arms nothing: there is no hedge left to place, and anything further
would be new risk.

`book()` returns the grid's own positions and the account's net from a single read.

### And the blind guard had to learn it too

Six sell orders he had just asked for were pulled by the blind guard, on the reasoning
that a fill nobody can watch is a fill nobody can close. That reasoning expired the
moment orders started being born with a server-side trailing stop: MetaApi protects
whatever those orders open, seen or unseen. The guard now stands down whenever
`server_trail()` is active, and says so in the message when it does fire.

## A brake measured against the account, not against another setting

`GRID_ADD_STOP_PCT` stops the grid expanding at 50% of `GRID_MAX_LOSS`. The owner set
`GRID_MAX_LOSS` to 99999 to take manual control of when the basket closes — and silently
turned the expansion brake off with it, because 50% of 99999 is a number no real
drawdown reaches.

The consequence showed up live: the account was $1,785 under water on a $3,919 balance,
and the grid was still posting "سدّيت فجوة — أضفت 2 أمر" every minute.

`GRID_EQUITY_BRAKE_PCT` (10%) is measured on **balance minus equity** — the whole
account, every open trade, whichever engine opened it, because margin does not care. At
or past that line the grid adds no level, lays no new ladder, and pulls what is already
armed. It derives from nothing the user can switch off except itself.

The lesson is about coupling, not about the number: **a safety limit must not be a
percentage of another safety limit.** Raising one should never silently disarm the other.

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

## الصفقة الرابحة ما تنغلق + ما يحط ستوب (نوفمبر 2026)

شكوى المالك: *«البوت لسه مايعطي اغلاق للصفقة الربحانه وايضا ما عطى ستوب لما عكس دولار»*.
الاثنين كانا خطأين حقيقيين بالتصميم، مو إعدادات غلط.

### 1. الرابحة رهينة الخاسرات

مع `GRID_SIDE_HARVEST=0` الحصاد يصير على **مجموع الشبكة**:

```python
groups = [("all", positions, [])]
here = pnl(group)                    # مجموع كل الصفقات
if here >= GRID_TP_CASH: ...          # لازم +5$
```

ومجموعه كان ≈ −1900$. يعني `here` ما راح يوصل `+5$` أبداً، فصفقة واحدة بـ+8$
داخل السلة **ما كان عندها ولا طريق للإغلاق**. و`GRID_TP_PER_TRADE` كان صفر،
فماكو هدف لكل صفقة يسندها.

الإصلاح: المحرك صار يكشف الحالة ويقولها — صفقة ربحها ≥ الهدف، والمجموع سالب،
وماكو هدف لكل صفقة — مع الأمر اللي يحلها (`/setkey gridtpone 5`). السكوت كان
أسوأ من الخطأ نفسه: هو كان يتفرج على صفقة رابحة تضيع وما يعرف السبب.

### 2. الستوب كان ينكتب على الأوامر الجديدة بس

`GRID_STOP_DIST` كان ينمرر مع الأمر لحظة إنشائه. يعني أي صفقة انفتحت **قبل**
ما يضبط الإعداد ضلت بلا ستوب، وهذا بالضبط اللي شافه: السعر عكس دولار على بيع
قديم وما صار شي.

`ensure_entry_stops()` صار يحط الوقف على الصفقات المفتوحة. الشرط الوحيد اللي
ما ينكسر: صفقة تجاوزت مكان الوقف من زمان (بيع صاعد عليها 20$) **ما ينحط عليها**،
لأن وقف بدولار هناك مو حماية — هو إغلاق فوري بأسوأ سعر. تنسكِب بتقرير، لا بصمت.
وصفقات الـ0.05 مال المالك (`777111/777222`) خارج النطاق تماماً.

### تصفير `gridstop` لازم يشيل الأوقاف القديمة

طلب المالك: *«بدون ستوب، فقط ستوب متابعة الربح»*. تصفير `GRID_STOP_DIST` كان
يمنع وقف **جديد** بس — والأوقاف اللي انكتبت فعلاً تبقى عند الوسيط، لأن الوسيط
يحفظ الوقف لحد ما شي يشيله. يعني كان راح يظن إنها انشالت وهي مسلّحة.

`clear_loss_stops()` تشيلها. والتصفير ضيّق بقصد: **بس** الوقف اللي واقف على جهة
الخسارة من سعر الدخول. ستوب متابعة الربح ما يقف هناك أبداً — هو موجود عند
التعادل أو بعده — فالتنظيف ما يقدر ياكل الحماية اللي يريد يبقيها.
و`stop_loss=0` هو اللي MT5 يقراه «شيل الوقف»؛ الـSDK يمرر الصفر الرقمي ويهمل
`None`.

## تشغيل نفس البوت على البيتكوين (نوفمبر 2026)

السمبول كان إعداداً جاهزاً، لكن أربع أشياء كانت مربوطة بالذهب ضمناً.

**1. مصدر السعر المجاني.** هذا أهمهن. المسار المجاني هو اللي نجّى البوت من أزمة
حصة MetaApi — بلاه كل تكّة تشتري سعراً مدفوعاً. `datafeed.py` كان يعرف الذهب
(Yahoo، PAXG، stooq) ويرمي أي اسم ثاني على Yahoo كما هو، و«BTCUSD» ما يشتغل
هناك. صار يروح على Binance (`BTCUSDT`) أولاً — مجاني، بلا مفتاح، شموع دقيقة
حقيقية، وما يغلق — ويرجع لـ Yahoo `BTC-USD` احتياطاً. و`bare()` تشلع زينة
الوسيط: `BTCUSD.` و`#BTCUSD` و`BTC/USD` و`BTCUSD_raw` كلهن نفس الزوج.

**2. المعايرة صارت لكل أداة.** كانت رقماً واحداً مشتركاً — كافي وقت ما كان
يتداول غير الذهب، بس تبديل السمبول كان راح يطبّق فرق الذهب على البيتكوين، أي
تخريب صامت لكل سعر يقراه البوت وقت ما الوسيط مخنوق. وحد «الفرق المعقول» كان
120$ ثابتاً: رقم ذهب. على بيتكوين قرب 100,000$ فرق 120$ شي عادي وكان راح
ينرفض كمستحيل فتبقى الأداة بلا معايرة للأبد. صار `max(120$, 0.4% من السعر)` —
يوسّع بس، وما يرفض شي كان يقبله.

**3. وحدات اللوت.** الذهب 100 أونصة باللوت، البيتكوين عملة واحدة. `/asset btc`
يضبط `GRID_CONTRACT=1` مع السمبول، لأن تركها على 100 يغلّط كل حساب هامش وكل
هدف نقدي وكل مسافة وقف — بصمت.

**4. حارس الخطوة.** وهاي الخطورة الحقيقية. خطوة 2$ على ذهب بـ4,000$ تعني
0.05%، و12 مستوى يمتدون 24$ — الذهب يحتاج ساعات يقطعهن. نفس الـ2$ على بيتكوين
بـ100,000$ تعني أن السلّم كله يمتد 0.024% من السعر: كل المستويات تمتلي بدقيقة
على سعر واحد عملياً، واللي يبقى مو شبكة — هي صفقة واحدة كبيرة بلا خطة.
`arm()` صار يرفض وينطي الرقم الصحيح. الأرضية (0.02% من السعر) مضبوطة تحت نسبة
الذهب نفسها بقصد، حتى إعداد الذهب الشغال ما يتأثر أبداً.

### اللوحة لازم تقول على أي أداة هي

المالك فتح `/grid` بعد `/asset btc` وقرا «المسافة 2$ · مدى الشبكة 12$» وظنها
بيتكوين. هي كانت أرقام الذهب — `/asset btc` ما كان كدر يمشي لأن الحساب لسه
يتجهّز عند MetaApi فما قدر يقرا قائمة الأدوات.

اللوحة كانت توصف الشبكة كلها بالدولار بدون ما تسمي الأداة ولا مرة. هذا ما كان
يضر وقت ما كان البوت يتداول ذهب بس. أول ما صار يقدر يتداول بيتكوين، «خطوة 2$»
صارت تعني شيئين مختلفين تماماً وما بالشاشة شي يفرّق. صارت تبدي:

```
• 🎯 الأداة: BTCUSD · اللوت = وحدة واحدة
• السعر 100,000$ · الخطوة 0.002% منه
• ⛔️ الخطوة صغيرة على هذا السعر — ما أنصب
  الحد 20$ · المنصوح 50$ · /setkey gridstep 50
```

السعر يجي من المسار المجاني، فاللوحة ما تكلّف ولا قراءة مدفوعة.

## ثلاثة أخطاء بحارس الكلفة (نوفمبر 2026)

المالك بعث لقطة: نفس السطر «السوق سكّر وعندك صفقة مفتوحة» يتكرر 5:41، 5:42،
5:43 — كل دقيقة. وقبلها بدقيقة واحدة أوقف الحساب ثم رجّعه. ثلاثة أخطاء، وكلهن
من نفس العائلة: **«ما أشوف» تُقرأ «ماكو شي»**.

**1. السطر كان يصفّر نفسه.** `warned_positions` ينضبط `True` عند التحذير، لكن
بالتكّة اللي بعدها نفس الظرف ما ينطبق على أي فرع فيروح للـ`else` — واللي كان
يصفّر العلم. فيحذّر، يصفّر، يحذّر. صار العلم ما ينمسح إلا لما يفتح السوق
فعلاً، وتحته ساعة صلبة (6 ساعات) حتى ما أي تسليك مستقبلي يرجع يسوي فيضان.

**2. غياب الاتصال كان يُقرأ «ماكو صفقات».** `our_positions_open()` كانت ترجع
`False` إذا `connection` مو موجود. هذا اللي أوقف الحساب الساعة 5:40 وصفقة حية
مفتوحة: الاتصال كان نازل لإعادة النشر، والحارس فهم السكوت كبراءة فنام، ثم صحا
بعد دقيقة لأن الشغل واضح إنه موجود. صارت ترجع `True` — نفس قاعدة مسار الاستثناء
اللي تحتها: لما ما نشوف، نبقى صاحين.

**3. التقويم كان تقويم ذهب.** `market_closed()` تحسب إغلاق الجمعة إلى الأحد.
البيتكوين ما عنده عطلة. ترك تقويم الذهب بعد التبديل كان راح يوقف الحساب كل سبت
وسط سوق حي يتحرك.

## الأداة لازم تكون معروفة بسطر واحد (نوفمبر 2026)

المالك بدّل للبيتكوين، التبديل فشل، وماكو مكان واحد بالبوت يقول هذا. فقرا لوحة
ذهب، وجاه تحذير عطلة ذهب، ونصب شبكة ذهب — وهو يضن إنه على بيتكوين. وأنا ضليت
أصلّح تقويم العطلة وحارس الكلفة، وهي أعراض، بدل الشي الوحيد اللي يهمه: إنه
ما يكدر يبدّل الأداة.

**الخطأ الجذري، وأنا كاتبه:**

```python
which = ASSET_WORDS.get(str(arg).strip().lower(), "gold")
```

أي كلمة ما يعرفها ترجع «ذهب». يعني `/asset <أي شي>` يدوّر على الذهب، يلكاه،
ويرد «✅ اسم أداة الذهب صحيح أصلاً» — **رسالة نجاح لتبديل ما صار**. الكلمة
المجهولة صارت رفضاً صريحاً، مو استبدالاً.

**وثلاث نقاط عمى انسدّت:**

- `whoami_line()` — الأداة، معنى اللوت، وحالة السوق بسطر واحد. تطلع مع كل
  اتصال، ومع `/grid on`، ومع `/asset` بدون وسيط.
- «شغّلت الشبكة — تنصب على السعر الحالي أول ما أرجع» انقالت يوم سبت على ذهب
  والسوق مغلق للأحد. صحيحة بالنهاية، ومضللة باللحظة. صارت تقول إن السوق مغلق.
- «ما لكيت أداة» كانت طريق مسدود. صارت تعرض الموجود فعلاً بالحساب مع أمر
  جاهز لكل اسم، حتى يعرف إذا وسيطه أصلاً يوفر العملة لو لا.

### ختم النسخة

ثلاث مرات انرفع إصلاح، والمالك قرا السلوك القديم على شاشته، ولا واحد منا يقدر
يعرف إذا الـ`git pull` صار لو لا — ماكو بالبوت شي يقول. صار يطبع أول سبعة
أحرف من الـcommit بنفس السطر اللي يقراه أصلاً. انتهى الالتباس.

### الختم لازم يناسب طريقة النشر الفعلية

المحاولة الأولى للختم سألت `git rev-parse`. وهي بلا فايدة على سيرفر المالك:
`/update` يسحب ملفات خام من GitHub، والمجلد أصلاً **مو مستودع git**. فالختم
كان راح يطبع «؟» بالضبط عند الشخص اللي انعمل لأجله.

صار يحسب بصمة `sha256` للملفات نفسها اللي `/update` يبدّلها. ما يحتاج git،
يتغير مع أي سطر يتغير، ونفس السبعة أحرف تنحسب من المستودع للمقارنة.

### اختيار الأداة الصح من بين سبعة

حساب المالك يعرض سبع أدوات فيها BTC: `BTCBCH` `BTCETH` `BTCLTC` `BTCO`
`BTCUSD` `BTCXAU` `GBTC`. كلهن بيتكوين، ووحدة بس هي اللي يتداولها. فسألته
يختار — وهذا آمن، بس الاختيارات اللي عرضتها كانت `/setkey symbol X`، وهذا
المسار **يتخطى ضبط حجم العقد والخطوة**: كان راح يصير على بيتكوين بعقد ذهب
(100 أونصة) وخطوة 2$.

صار: الزوج المقابل للدولار يُختار تلقائياً (نفس منطق `GOLD_CROSSES`)، والقائمة
— لو احتاجها — تعرض `/asset X` اللي يضبط كل شي، و`/asset` صار يقبل اسم الوسيط
الحرفي مو بس الكنية.

وبنفس التعديل وقعت بفخ `global` بعد الاستخدام للمرة الثالثة. `compile()` مسكها،
`ast.parse` ما كان راح يمسكها — ولهذا كل فحوص المشروع تستخدم `compile()`.

## أهدأ خطأ بالمشروع كله (نوفمبر 2026)

المالك ضبط البيتكوين صح، أرسل `/grid down` ثم `/grid on`، والبوت رد «🕸 شغّلت
الشبكة». وما طلع ولا أمر على الجارت.

السبب: `GRID_ENABLED=1` بس `BOT_ACTIVE=0` من `/pause` سابق. والمحرك:

```python
if not (GRID_ENABLED and is_active()):
    await asyncio.sleep(3)
    continue          # للأبد، وبصمت تام
```

ماكو مكان يربط الاثنين. اللوحة تقول «🔴 النظام: متوقف» بزر منفصل، والرسالة
تقول «شغّلت الشبكة» — والمستخدم ما عنده سبب يربطهن.

صار: المحرك يقول السبب مع الأمر اللي يحله (مرة كل 15 دقيقة، لا فيضان)،
و`/grid on` يحذّر باللحظة اللي يكتب بيها.

### وملاحظة على طريقة عملي

التعديل انكتب مرتين. أول مرة كان ضمن سكربت فيه ثلاث استبدالات، وحدة منهن فشلت
فاستدعت `sys.exit` قبل سطر الحفظ — يعني **ولا واحد انحفظ**، وأنا كملت على أساس
إن الأول نجح لأنه طبع `ok`. الاختبار هو اللي مسكها. الدرس: السكربت اللي يحفظ
بالنهاية إما ينجح كله أو ما ينفع الاعتماد على أي `ok` طبعه قبل الفشل.

## «راجع اسم الأداة وحجم اللوت» — تخمين مكان الجواب

أول محاولة نصب على البيتكوين فشلت بكل المستويات، والرسالة كانت:

> ما انحطت ولا أمر — راجع اسم الأداة وحجم اللوت عند وسيطك

الوسيط كان قايل السبب بالضبط، كل مرة، والبوت يرميه بملف اللوج — واللي المالك
ما يكدر يقراه من تلفونه — ويطبع تخمين مكانه. صارت الرسالة تحمل رد الوسيط
الحرفي (أول ثلاثة أسباب مختلفة، بدون تكرار)، والإعدادات الفعلية المستخدمة.

والسبب الأرجح هنا: `GRID_STOP_DIST=1` — دولار واحد. على الذهب وقف معقول، على
البيتكوين **داخل السبريد نفسه** (17$)، فالوسيط يرفض كل أمر يحمله. فصار تبديل
الأداة يلغي الوقف الثابت ويقول إنه ألغاه: مسافة سعر من أداة ثانية ما لها معنى،
وستوب متابعة الربح ما يتأثر.

### «ماكو اتصال» بعد إعادة تشغيل = انتظار، مو عطل

`/grid rearm` بعد ثواني من إعادة التشغيل رد «ماكو اتصال حالياً — ما أقدر ألغي
الأوامر القديمة بأمان». والاتصال وصل بعدها بثواني. الرفض الجاف يُقرأ كعطل
ويخلي المالك يدوّر على سبب مو موجود. صار ينتظر لحد 20 ثانية، وإذا ما جهز يقول
«لسه ما جهز، جرّب بعد شوي» — ويضيف إن `rearm` أصلاً مو للبدء: المحرك ينصب
لحاله أول ما يتصل، و`rearm` للتوسيط من جديد.

## `/spec` — اسأل الوسيط بدل ما تخمّن

ثلاث جولات ضاعت بالتخمين على سبب عدم نصب ولا أمر على البيتكوين: أقل لوت؟ خطوة
اللوت؟ حجم العقد؟ أقرب وقف مسموح؟ كل هذي الأجوبة موجودة بمواصفات الأداة عند
الوسيط — نداء واحد — وإحنا نقرا لقطات شاشة.

`/spec` يجيب مواصفات الوسيط ويقارنها بالإعدادات الفعلية، وكل تعارض يطلع مع
الأمر اللي يصلحه جاهز:

- لوت أصغر من حد الوسيط، أو مو من مضاعفات خطوته
- حجم عقد مختلف عن اللي عند الوسيط
- وقف ثابت أقرب من `stopsLevel`
- **تريلينغ مقيس بالدولار لكن محسوب بالسعر**: `0.5$` على لوت `0.02` بعقد `1`
  = `25$` سعر. على الذهب نفس الرقم = `0.25$`. الفرق مئة ضعف، والوسيط يرفض
  بالسعر مو بالدولار.
- خطوة أصغر من أرضيتها عند هذا السعر

## صفقات الأداة القديمة بقت مرئية بعد التبديل (نوفمبر 2026)

`/grid rearm` على البيتكوين رد: «تركت **4 صفقة** مفتوحة مثل ما هي». وماكو ولا
صفقة بيتكوين — هاي صفقات شبكة **الذهب**.

كل قراءات الشبكة كانت تفلتر بالـ`magic` فقط، بدون الأداة. ما ظهر هذا ولا مرة
لأن البوت ما بدّل أداة قبل اليوم. والنتائج:

- الأربعة ياكلون من سقف الصفقات المفتوحة
- وداخلين بحساب ربح السلة — **سلة بـ−2000$ ما تنحصد أبداً**، فصفقة بيتكوين
  رابحة تبقى رهينة خسارة ذهب
- ووقف السلة يقيس خسارة الذهب
- و`/grid off` چان **يغلق صفقات الذهب** وانت على البيتكوين

صار `ours()` تشترط الأداة، و`grid_orders` و`cancel_all` مثلها.

### ومصدران للحقيقة

المحرك ياخذ الأداة **كوسيط**، والفلاتر كانت تقراها **من البيئة** بشكل مستقل.
قراءتان لنفس الإعداد تتفقان لحد ما تختلفان: مستدعي يمرر `XAUUSD` والبيئة تقول
`GOLD` يخلي كل صفقات الشبكة غير مرئية — والشبكة تملك لا شي بصمت. صار
`set_symbol()` والمحرك ينشر الأداة اللي انعطت له فعلاً.

### والوسيط الورقي كان يسمّي كل صفقة بأداته الافتراضية

`broker_paper` يتجاهل الأداة الممررة ويحط `ENGINE_SYMBOL` على كل صفقة وأمر.
انكشف لأن الاختبارات صارت تفلتر بالأداة. صار يحترم اللي ينعطى له، والأمر
المعلّق لما ينفذ يصير صفقة على أداته هو.

### والمكبح صار يقول من وين الهبوط

المكبح يقيس هبوط **الحساب** كله. على البيتكوين كان يرفض النصب بسبب عائم
الذهب — والمالك يشوف بوت يرفض الشغل على أداة ما خسر عليها فلس. صار يقول كم من
الهبوط من أدوات ثانية، ويعطي رقم المكبح اللي يتجاوزه.

## `/why` — اسأل المحرك ليش واقف

بعد كل الإصلاحات، المالك حدّث للنسخة الصحيحة وظلت الشبكة فارغة — **وبدون ولا
رسالة خطأ**. والسكوت أسوأ من الخطأ: ماكو شي ينقال ولا شي ينفحص.

أربع جلسات راحت باستنتاج سبب التوقف من لقطات شاشة. وكل بوابة يمر بيها المحرك
رخيصة الحساب عند الطلب — يعني كان ممكن **نسأل البوت** من أول يوم.

`/why` يمشي بنفس سلسلة القرارات بالترتيب ويعلّم ✅/❌ على كل وحدة، ويقول «أول
مانع» مع الأمر اللي يحله: الشبكة مشغّلة؟ النظام شغّال؟ أكو اتصال؟ موقوفة؟
منصوبة أصلاً؟ تحت سقف الصفقات؟ المكبح؟ الخطوة فوق أرضيتها؟

وكل سطر هو **الشرط الحقيقي نفسه**، مو وصف له.

### وبوابة ساكتة انسدّت

```python
if state.get("halted"):
    await asyncio.sleep(5)
    continue          # للأبد، بصمت
```

إيقاف انضبط قبل ساعات على أداة ثانية يوقف المحرك ميّت وماكو شي على أي شاشة
يقول ليش. صار يقول السبب المسجّل والأمر اللي يلغيه.

## الفشل الواحد كان يوقف الشبكة للأبد (نوفمبر 2026)

`/why` طلّع السبب بسطر واحد:

> ❌ الشبكة موقوفة — بسبب: «ما انحطت ولا أمر — راجع اسم الأداة وحجم اللوت»

محاولة نصب فشلت الساعة 6:15 — سببها وقف بدولار واحد، رقم ذهب بقى على
البيتكوين. والكود كان يسوي هذا:

```python
ok, err = await arm(conn, symbol, notify)
if not ok:
    state["halted"] = True      # للأبد
```

**دفعة مرفوضة واحدة = إيقاف دائم.** وبعدها انصلح الوقف، وانصلح حجم العقد،
وانصلحت الخطوة، وانصلح المكبح، واتصلحت فلترة الأداة — وولا واحد منهن غيّر شي،
لأن الإيقاف عاش فوقهن كلهن بصمت. ساعات راحت بمطاردة إعدادات كانت صحيحة أصلاً.

صار: الرفض يُعاد، مو يُقفل. عدّاد يُصفّر عند أول نجاح، وإيقاف حقيقي بعد
`ARM_FAIL_HALT` (10) رفضات متتالية — مع قول صريح إنه لسه يحاول، حتى إذا صلّح
المستخدم الإعداد ينصب لحاله بدون أي أمر.

### والدرس الأكبر

خمس جولات راحت وأنا أقرا لقطات شاشة وأخمّن. لما أخيراً أعدت إعداداته عندي
وشغّلت المحرك الحقيقي، السبب ظهر بدقيقتين. **المحاكاة المحلية كانت متاحة من
الساعة الأولى.** و`/why` — اللي حسمها نهائياً — كان ممكن ينكتب بعشر دقائق.

## أزرار الشبكة

كل تحكّم بالشبكة كان أمراً مكتوباً. المالك لازم يتذكر `gridlot` و `gridmax`
و `/grid both` حتى يغيّر لوت أو اتجاه — وهذا سبب كافي لوحده إن الإعداد يطلع
غلط. صار صف أزرار باللوحة نفسها:

```
🕸 الشبكة: شغالة        ↔️ الجهتين
➖ لوت    اللوت: 0.02    ➕ لوت
🔄 إعادة نصب            🧭 ليش ما ينصب
```

وثلاث مزالق انسدّت بالتنفيذ:

- **تشغيل الشبكة من الزر يلغي الإيقاف** المتروك من دفعة مرفوضة — نفس الشي
  اللي كلّف المالك ساعات لما كان لازم يعرف إن `/grid on` هو اللي يمسحه.
- **رفع اللوت يرفع السقف معه.** `GRID_BASE_LOT=0.05` مع `GRID_MAX_LOT=0.03`
  يعني السلّم يبقى على القديم بصمت — وهذا حصل فعلاً أول مرة غيّر اللوت.
- **خطوات اللوت قائمة محددة** (0.01 → 0.02 → 0.03 → 0.05 …) مو جمع حسابي،
  حتى ما تطلع قيمة خارج خطوة الوسيط فيرفض كل أمر.

## الأرضية كانت تحط الوقف فوق السعر نفسه

المالك طلب «يحجز ربح دولار ويتابع». وأنا جربت الإعداد بالمحاكاة قبل ما أرسله —
وهناك ظهر خلل بإعداده **الحالي**:

```
ربح 2.0$  →  يقفل 2.0$     ⚠️ الوقف عند السعر نفسه
```

الأرضية كانت تتسلّح عند نفس الرقم اللي تقفله: «لما توصل 2$ احتفظ بـ2$» يحط
الوقف بالضبط وين السعر، فأول تذبذب ضدها يغلق الصفقة **باللحظة اللي استحقت بيها
الحماية**. الأرضية لسه تقفل اللي تقوله؛ بس صارت تنتظر فراغاً بسيطاً قبل ما
تتسلّح (`FLOOR_HEADROOM_CASH`، افتراضياً ربع الأرضية).

### ملاحظة على الاختبار

`test_servertrail` كان يثبّت `{threshold: 2.0, stopLoss: 2.0}` كمواصفة — يعني
**جمّد الخلل**. قاعدة المالك عن *اللي ينحفظ*، مو عن لحظة كتابة الوقف. صار
الفحص على القصد: يقفل 2$، ويتسلّح بعد فراغ.

## تغيير قاعدة الحماية ما كان يوصل الصفقات الشغالة

المالك غيّر التأمين من «يقفل 0.10$» إلى «يقفل 1$». ونزل الإعداد صح — بس الصفقات
اللي كانت شغالة **ضلت على القاعدة القديمة للأبد**، لأن الفحص كان:

```python
if has_server_trail(p):      # أكو تريلينغ؟ خلاص
    continue
```

«أكو تريلينغ» مو «أكو التريلينغ الصح». وقاعدة يقدر يغيّرها لازم تنتشر لما تتغيّر.
صار الفحص يقارن الأرقام نفسها، والصفقة على قاعدة قديمة تنترقّى ويطلع إشعار.
والشكل اللي ما ينقرا (الوسيط ممكن يرجّع أي شي) يُترك كما هو بدل ما يُعاد كتابته
كل تكّة.

## والفراغ كان يعتمد على إعداد ما له علاقة

حسبته `max(ربع الأرضية، عتبة التأمين)`. مع أرضية 2$ وعتبة 1.2$ صار يتسلّح عند
**3.20$** — مو 2.50$ اللي قلتها للمالك بنفس الرسالة. قاعدة حماية ما يقدر
يتوقعها من أرقامه هي قاعدة ما يثق بيها. صارت ربع الأرضية وبس.

واختباري كتب القيمة `2.5` حرفياً، فما لاحظ الفرق — صار يبنيها من الإعداد الحي.

## مراجعة خمس مرات (نوفمبر 2026)

خمسة مرورات، كل واحد بعدسة مختلفة وبفحص مُنفَّذ مو قراءة بالعين. خمسة أخطاء
حقيقية — وأخطرهن ما كان ظاهراً بأي شاشة.

### 1. الوسيط الورقي يعلن «الربح صفر» لما ما يقدر يسعّر

```python
except Exception:
    floating = 0.0
```

صفر هنا **تصريح عن السوق**، والحقيقة «ما كدرت أسعّر». الفرق مهم: مكبح رأس
المال يقرا هذا الرقم، والصفر يقوله إن الحساب سليم وهو ممكن يكون غارق. صار
يمسك آخر قيمة حسبها فعلاً ويسجّل الفشل. نفس العائلة اللي نامت فوق صفقة حية.

### 2. الوسيط الورقي بعقد الذهب والمحرك على البيتكوين

`PAPER_CONTRACT_SIZE` افتراضيه 100 بينما `GRID_CONTRACT` صار 1 — يعني كل ربح
تجريبي **مئة ضعف** الحقيقي. حساب تجريبي يكذب لصالح المالك أسوأ من ماكو حساب
تجريبي، لأنه راح يحجّم صفقات حقيقية منه. صار يعكس الحي.

### 3. مسافة التنصيف رقم ذهب

`HALF_DISTANCE=5$` على ذهب بـ4,000$ = عُشر بالمئة. نفس الـ5$ على بيتكوين
بـ66,000$ = 0.008%، يقطعها السوق بثواني: كل صفقة خاسرة تنتصف فوراً ومتكرر لحد
أصغر لوت. صارت تحافظ على **نسبتها** من السعر مثل خطوة الشبكة.

### 4. مسار «غير منصوبة» يحرق الحصة

الحلقة الرئيسية مُنظَّمة بـ`reads_free()`، بس مسار الشبكة غير المنصوبة كان
ينام 2–5 ثواني ثابتة وهو يقرا الكتاب كل مرة: 10,800 قراءة/6 ساعات مقابل حصة
≈4,320. نفس شكل الانقطاع اللي أعمى البوت قبل، بس على المحرك الثاني.

### 5. سقف الصفقات ما كان سقفاً

بالمحاكاة على تأرجح حقيقي: **9 صفقات مفتوحة والسقف 8**. السبب إن النصب يحط
6 مستويات × جهتين = **12 محفّز** بالسوق، والسحب عند الامتلاء تفاعلي — يتأخر
تكّة، والبيتكوين يقطع عدة مستويات بالتكّة. صار النصب ما يترك محفّزات أكثر من
السقف. بعد الإصلاح: 8 بالضبط.

### ملاحظة على الاختبارات

ثلاثة اختبارات طاحت بعد الإصلاح لأنها كانت تثبّت **السلوك القديم كمواصفة**:
12 أمر معلّق، ونص حرفي لعبارات `sleep`. اختبار يقيس كيف انكتب السطر مو شنو
يسويه راح ينكسر على كل تحسين، ويعلّمك تتجاهله. انعدّلوا ليفحصوا القاعدة.

## إغلاق على ارتداد نسبة من القمة

طلب المالك: «يحجز نص دولار ويتابع مع الصفقة لحد ما ترتد 15% من قيمتها يغلق».

ستوب الوسيط يقدر يمسك **مسافة ثابتة** بس — نسبة من القمة لازم تتحسب هنا، تكّة
تكّة. فالاثنان يشتغلون سوا وماكو واحد زائد: `TRADE_GIVEBACK_PCT` أضيق ويتحرك
أول وقت ما البوت حي، وقفل الوسيط تحته يصمد لو البوت مو حي.

ونقطة مهمة: نسبة من قمة صغيرة ممكن تنزل تحت المقفول — 15% من قمة `0.55$` هي
`0.47$`، يعني ترجّع جزء من الـ`0.50$` المضمونة. فالأرضية `BE_LOCK_CASH` دائماً
تحكم.

| قمة الصفقة | يغلق عند |
|---|---|
| 0.60$ | 0.51$ |
| 1$ | 0.85$ |
| 5$ | 4.25$ |
| 10$ | 8.50$ |

### وقاعدة انحرست

الإغلاق انحط أول مرة **فوق** `apply_breakeven`، يعني بمنطقة تضييق الوقف. و
`test_protect` يحرس تمييزاً حقيقياً: **تضييق الوقف آمن بأي حالة ولازم يستمر
والنظام موقوف؛ الإغلاق فعل ولازم ما يستمر.** انتقل تحت، جنب `soft_protect`،
وبنفس إذن `bot_active`.

واختبار القاعدة كان يعدّ `bot_active` مرة واحدة — يثبّت **عدد المغلقات** مو
القاعدة، فانكسر أول ما انضاف ثاني. صار يحلّل النداءات بـ`ast` ويفحص إن كل
مُغلِق يحمل الإذن.

## الهدف ما كان يتحرك لما يتغيّر رقمه

المالك قال الصفقات ما تنغلق على ربح. والسبب رقم: `GRID_TP_PER_TRADE=5` بلوت
`0.01` وعقد `1` يعني **500$ حركة بالبيتكوين** لكل صفقة — هدف ما يوصله إلا
نادراً، فبدا كأن البوت ما يغلق أبداً.

وتحته علة ثانية، نفس عائلة التريلينغ:

```python
if p.get("takeProfit"):
    continue          # عليها هدف؟ خلاص
```

«عليها هدف» مو «عليها الهدف الصح». يعني لو غيّر الرقم من 5$ إلى 1$، كل صفقة
شغالة تبقى على الهدف القديم البعيد **للأبد**. صار يقارن المسافة الفعلية
وينقل الصفقة للرقم الجديد ويقول كم نقل.

### الهدف بلوت 0.01

| الهدف | حركة البيتكوين |
|---|---|
| 0.50$ | 50$ |
| 1.00$ | 100$ |
| 2.00$ | 200$ |
| 5.00$ | **500$** ← اللي كان مضبوط |

## الإعدادات بالدولار تغيّر معناها بصمت لما يتغيّر اللوت

المالك قال إن «الهدف يتابع الصفقة» بان بلا فايدة. وهو محق، والسبب رقم مخفي:
`betrail 3$` كان اختياراً معقولاً على لوت `0.02`. نزّل اللوت إلى `0.01`،
فنفس الـ3$ صارت **300$ حركة بالبيتكوين** — التريلينغ ما يلحق يتحرك قبل ما
يرجع السعر.

التسعير بالدولار كان مقصوداً: يخلي القاعدة مستقلة عن حجم اللوت. لكن معناها
كـ**مسافة** يتغيّر عكسياً مع اللوت، وماكو شاشة تقول هذا.

صارت اللوحة تبدي التحويل والحكم:

```
⚖️ كل 1$ ربح = حركة 100$ بهذا اللوت
• التأمين: 0.6$ = حركة 60$   ✅
• التريلينغ: 3$ = حركة 300$  ⚠️ واسع
  وكل سلّم الشبكة 198$ — ما يلحق يتحرك داخل مدى الشبكة
```

### والحكم مثبّت على الإعداد مو على نسبة مخترعة

أول محاولة حكمت بنِسب ثابتة من السعر (0.4% واسع، 0.03% ضيق) — وصنّفت إعدادات
الذهب **الشغالة فعلاً** كـ«ضيقة». النسبة المخترعة تقيس خيالي مو الواقع.

المقياس الصادق هو هندسة الشبكة نفسها: **مسافة حماية أوسع من كل السلّم ما تقدر
تتحرك داخل المدى اللي تتداول فيه الشبكة** — وهذا صحيح على أي أداة بلا أي رقم
سحري.

## الشبكة كانت تصفن بعد ما تغلق صفقاتها

شكوى المالك: «ورا ما يغلق الصفقات يصفن، ما بي حيوية، يرجع ينصب».

إعادة التوسيط كانت تحتاج الكتاب **فاضي تماماً** — لا صفقات ولا أوامر:

```python
if not positions and not orders:
    state["armed"] = False      # الحالة الوحيدة اللي يعيد النصب بيها
```

فالحالة الاعتيادية ما كانت مغطاة: الأهداف تغلق الرابحات، السعر يمشي، ويبقى
حفنة أوامر معلّقة **ورا السوق**. و`refill_gaps` يرقّع ثغرة وحدة لكل جهة بالتكّة
— ترقيع، مو إعادة توسيط. الشبكة تقعد تبيّن ميتة، وهي فعلاً ميتة: ماكو شي
تملكه يقدر يوصله السعر.

صار يعيد التوسيط لما **يبتعد السعر `GRID_RECENTER_STEPS` خطوات عن المركز**
وماكو أي مستوى على مسافة إمساك. بشرطين:

- **ماكو صفقة مفتوحة** — سحب أمر معلّق وأكو صفقات شغالة ممكن يشيل مستوى
  يحوّط وحدة منهن
- **وماكو مستوى قريب أصلاً** — لو السلّم لسه يمسك السعر، إعادة التوسيط
  طحن بلا فايدة

## تبديل الأداة كان باتجاه واحد

الرجوع للذهب كشف الخلل: التصحيح كان مشروطاً بالوجهة.

```python
if which in ("btc", "eth"):
    write_env_value("GRID_CONTRACT", "1")
```

رايح للبيتكوين؟ يضبط. راجع للذهب؟ **يترك عقد البيتكوين `1` وخطوته `33$`** —
كل حساب هامش وكل هدف نقدي وكل مسافة وقف غلط بمئة ضعف، بصمت.

والعلاج مو إضافة فرع للذهب. العلاج إلغاء تمييز الوجهة كلياً و**سؤال الوسيط
شنو هي هذي الأداة**: `contractSize` و`minVolume` من المواصفات، والخطوة نصف
بالألف من سعرها. يشتغل على أي أداة، وما يحتاج أحد يتذكر يضيف حالة.

وإذا الوسيط ما جاوب، ما ينكتب حجم عقد مخمّن — تُترك كما هي ويُسجّل السبب.

### واختباران كانا يثبّتان الرقم

`test_btc` و`test_btcpick` يفحصان `write_env_value("GRID_CONTRACT", "1")`
حرفياً — يعني يحرسان **التثبيت نفسه** اللي سبب الخلل. وسطاؤهما الوهميون ما
كانوا يجاوبون عن المواصفات أصلاً. انضاف لهم الجواب، وصار الفحص على المصدر.

## التصفية الممولة — فكرة المالك

طلبه بكلامه: «إذا علقت عندنا صفقة وتظل عائمة، أي وقت ما البوت يحقق خسارتها
يغلقها — الربح يطلع صفر».

يعني الصفقة العالقة ما تنقص بخسارة ولا تُترك تتعفن: **تُدفع من اللي كسبته
الشبكة أصلاً**.

والتنفيذ أبسط مما يبدو: الصندوق هو `realized` نفسه — صافي ربح الشبكة المحصّل.
وإغلاق الخاسرة يسجّلها، فالصندوق **ينزل بالضبط بقيمة ما كلّف**. ماكو دفتر
ثاني ولا رصيد يُمسك باليد.

| | |
|---|---|
| متى تُعتبر عالقة | ابتعد عنها السعر `RESCUE_STEPS` خطوات (افتراضي 3) |
| أيها أول | **الأكبر خسارة** اللي يغطيها الصندوق — يشيل أكبر عبء |
| كم بالمرة | وحدة — ثم يعيد القراءة بدل ما يشتغل على كتاب صار قديم |
| محجوز | `RESCUE_RESERVE` ما ينصرف أبداً |

مطفية افتراضياً. الرابح ما تلمسه، والخاسرة اللي لسه قرب السعر تُترك ترجع
لحالها، وتحترم `/pause`.

### وملاحظة على الفحص

كتبت أرقام الاختبار باليد: صفقة «صغيرة» على بُعد 5.82$ والعتبة 6$ — يعني مو
عالقة أصلاً، والفحص كان يطالب بتصفية **ما يجوز** تصير. صارت الأرباح تُحسب من
سعر الدخول، فما تقدر تنفصل عن هندسة الشبكة.

## `/trades` — صفقة صفقة، بدون تخمين

المالك بعث لقطة: صفقة رابحة بـ`+3.02$` قاعدة على الشارت بلا وقف. وولا واحد
منا يقدر يقول ليش من صورة.

أعدت لوحته بالضبط بالمحاكاة وشغّلت خط الحماية الحقيقي عليها. النتيجة: الكود
**حط الوقف** على الرابحة عند `3859.30` — يعني الكود صح، واللي على سيرفره مو
هو. بس «حدّث وخلص» مو جواب: المطلوب شي يقول الحقيقة **عن كل صفقة بعينها**
بدون ما أخمّن من لقطة.

`/trades` يطبع لكل صفقة: مين يملكها (شبكة / ارتكاز / مسلّمة إلك / مو من
البوت)، وقفها وهدفها، إذا التريلينغ عند الوسيط **حالي أو قديم**، وشنو
القاعدة الجاية بالضبط — «التأمين بعد 0.3$ حركة»، «ارتداد يغلقها عند 2.57$».

ويطبع النسخة الشغالة تحته، فسؤال «هل وصل التحديث؟» ينحسم بنفس الرسالة.

## أهدأ بوابة بقيت: الحماية مطفية

المالك بعث لقطة ثالثة: صفقات رابحة بلا وقف. أعدت لوحته بالمحاكاة وشغّلت خط
الحماية الحقيقي — **حط الوقف على الرابحة**. يعني المنطق صح.

فليش ما يشتغل عنده؟ بوابة بأول الحلقة:

```python
if not (connection and grid.BE_ENABLED):
    await asyncio.sleep(3)
    continue          # للأبد، بصمت تام
```

`BE_ENABLED=0` يطفي **كل** وقف وتريلينغ وتأمين وهدف تملكه هاي الحلقة، للأبد،
وماكو شي يقول. والمستخدم يشوف صفقة رابحة مكشوفة وما عنده طريقة يفرّق بين
حارس **مطفي** وحارس **معطّل**.

صار يقولها (مرة كل ربع ساعة)، و`/why` يفحصها ضمن بواباته.

### وبدون قراءة صفقات

أول نسخة من الإشعار كانت تعدّ الصفقات المكشوفة — يعني `get_positions` ثانية
بنفس الحلقة. و`test_quota` مسكها فوراً: القراءة المكررة بالتكّة هي اللي حرقت
حصة الست ساعات أول مرة. التحذير ما يحتاج رقم حتى يكون صادقاً.

### واختباران كانا يقيسان نصاً

`test_quota` يعدّ كلمة `get_positions` بالنص — فعدّ **تعليقي** اللي يشرح ليش
ماكو قراءة ثانية واعتبره قراءة. صار يجرّد التعليقات ويعدّ النداءات. والفحص
اللي بعده يدوّر على نص التعليق نفسه، فينطبق على النص غير المجرّد.

## ما جاب المراجع الخارجي (نوفمبر 2026)

### 1 · كلفة الدورة — الرقم الغائب

أدق نقطة بالمراجعة: **هدف أصغر من أضعاف كلفة الدورة هو صفقة خاسرة بثوب قاعدة
رابحة**، مهما كان باقي السلوك صحيحاً. وما كان هذا الرقم مذكوراً ولا مرة.

بأرقام المالك: سبريد `0.32$` ذهب × لوت `0.02` × عقد `100` = **`0.64$` لكل دورة**
— يعني **38%** من خروج نموذجي عند قمة `2$`. صار يطلع على اللوحة مع حكم.

### 2 · المكبح كان يقرا الرصيد — والرصيد يتغيّر

`balance - equity` تشوف **العائم بس**. إغلاق الخاسرة يحوّلها لرصيد فينزل الرصيد
ويصير الفرق صفراً — **فباللحظة اللي ينضرب بيها الحساب أكثر، المكبح يقول سليم**.

وحساب المالك أثبتها: سكّر ≈1,886$ خسارة ذهب، نزل الرصيد لـ1,885$، وقرا الهبوط
**صفر** على حساب خسر نصه. نفس عائلة «ما أشوف تُقرأ ماكو شي» — **وخمسة مرورات
مراجعة ما مسكتها**.

صار القياس من **أعلى حقوق وصلها الحساب** (`equity_hwm`)، والخسارة المحققة تبقى
محسوبة. والقمة تنجو من `reset()`: هبوط يقدر المستخدم يمسحه بإعادة نصب مو هبوط،
هو رقم يجامل اللي ينظر له.

### 3 · `/perf` — الحكم من تاريخ الحساب مو من backtest

كل اختبار بالمشروع يفحص إن **الكود يسوي اللي انقاله**. ولا واحد يقول إن
**قول هذا يربح**. والـbacktest يجاوب بتعبئة مفترضة وسبريد مفترض؛ بينما تاريخ
الحساب يجاوب بالتعبئة اللي صارت فعلاً، عند هذا الوسيط، بهاي الإعدادات. أسرع
بناءً وأصدق، فجاء أولاً.

### دروس هندسية من نفس الشغل

**دالة تبدو حسابية وتكتب على القرص فخ.** أول نسخة من `drawdown_brake` كانت
تحدّث القمة وهي تقرا — يعني مجرد السؤال «هل إحنا مكبوحين؟» يحرّك القمة.
واختباران نادوها بأرقام مخترعة فعلّما الملف الحقيقي قمة ما صارت أبداً. انفصلت:
`note_equity()` تسجّل عند قراءة الحساب فعلاً، و`drawdown_brake()` تقرا بس.

**تلوّث حالة بالمستودع.** `grid_state.json` ما كان بـ`.gitignore`، فاختبار نسى
يحوّل `STATE_PATH` كتب فيه قمة وهمية، وانرفعت مع الكود. انضاف للتجاهل.

**تنسيق رقم اختير على أداة.** `{:,.0f}` انكتب أيام البيتكوين حيث المسافات
بالمئات، فحوّل نصف دولار الذهب إلى `0$` — رقم صحيح يُعرض غلطاً، على السطر
المخصص لشرح التحويل. `money()` تختار الدقة حسب الحجم.

### «ماكو حماية للصفقة» — السؤال اللي ما كان ينجاوب من صورة

ستّ جولات انصرفت على تخمين سبب جلوس صفقة رابحة بلا وقف، وكل مرة من صورة شاشة
الشارت. والشارت **ما يكدر** يجاوب: ما يميّز صفقة فتحها البوت من صفقة فتحها
المالك بيده. والحماية تفلتر على رقم الـmagic — فصفقة يدوية ما تاخذ وقف ولا
تريلينغ ولا خروج، **وما تگول شي**. الاستثناء صحيح؛ الصمت هو الخطأ: خلّى قاعدة
مقصودة تنقرا مثل حارس خربان.

صارت ثلاث حركات:

1. **`warn_unmanaged`** — أي صفقة رابحة على رمزنا ما نحميها، البوت يحچي عنها:
   كم ربحها، وليش ما يلمسها، وشنو الأمر اللي يغيّر هذا. ما تكلّف ولا قراءة
   زيادة؛ القائمة أصلاً بالذاكرة.
2. **`/adopt <رقم>`** — المالك يسلّم صفقة فتحها بيده للبوت. مو تلقائي أبداً:
   الحماية تعني صلاحية **الإغلاق**، والبوت ما يفترض هاي الصلاحية على فلوس
   غيره حطها بالسوق. رسالة التأكيد تگول صراحةً إنه صار يكدر يسكّرها.
   و`/unadopt` يرجّعها.
3. **`/trades` يطبع رقم كل صفقة** — ما كان يطبعه، فالأمر الجديد ما كان عنده
   من وين يجيب الرقم.

التبنّي ينجو من `reset()` (مثل التسليم بالضبط)، والتسليم يغلب التبنّي، وتبنّي
صفقة مسلَّمة ينظّف التناقض بدل ما يتركه.

**ودرس اختبار:** `test_protect` كان ينام 1.2 ثانية وبعدين يفحص. تحت حمل، الحلقة
تاخذ دورة وحدة بدل دورتين فيطلع أحمر — **يعني بالضبط مثل حارس خربان**. صار ينتظر
الشرط نفسه بحد أقصى ثماني ثوانٍ. اختبار يفحص الساعة مو السلوك يكذب بالاتجاهين.

### «ماكو صفقات» ما تكفي — على أي حساب؟

`/trades` رجّع «ماكو صفقات مفتوحة على XAUUSD» بينما شارت المالك قبلها بنص ساعة
كان بيه صفقتين. احتمالان: سكّرهن، أو **البوت يقرا حساباً غير اللي ينظر له**.
والتقرير ما كان يفرّق — نفس الجملة تطلع بالحالتين.

هذا نفس عائلة الأخطاء اللي تطارد المشروع من أوله: *«ما أشوف» تُقرأ «ماكو شي»*.
صار التقرير يكتب رقم الحساب والسيرفر والرصيد فوق كل جواب، ويگول للمالك يقارنه
بالـMT5.

والفلتر على الرمز كان يخفي الباقي: حساب بيه صفقات على `XAUUSD.` أو `GOLD` يُقرا
فاضياً تماماً. صار يفرّق بين «الحساب فاضي» و«فاضي من هذا الرمز، وهاي اللي بيه»،
ويدلّ على `/asset` إذا طلع نفس الذهب باسم ثاني.

### الصفقة المنسية على أداة ثانية

تسجيل شاشة من المالك بيّن صفقة `BTCUSD buy 0.05` مفتوحة بحساب البوت بينما البوت
راجع يشتغل على الذهب. بقية من يوم بدّلنا الأداة: الفلترة على الرمز، فمن تلك
اللحظة **ما حد يراقبها ولا يحط عليها وقف ولا حتى يذكرها**. كانت تظهر بمكان واحد
بس — داخل رسالة مكبح الحقوق، يعني بعد ما تصير المشكلة.

صار `/trades` يعدّد صفقات الأدوات الثانية بقسم خاص مع مجموعها، والبوت يذكّر بيها
من نفسه كل ست ساعات. ما يسكّرها — مو صفقته — بس ما يخليها مخفية.

### القمم كانت تنمسح بكل `/update`

ثلاث حمايات تُقاس من **أفضل ربح وصلته الصفقة**: الخروج على الارتداد، وقفل
التعادل، والخروج الناعم. الثلاثة كانوا يقروا من `dict` بالذاكرة يموت مع العملية.
وكل `/update` يعيد تشغيل البوت — وأنا اللي أطلب منه يسوي `/update` كل شوي.

النتيجة بالأرقام: صفقة وصلت `+5$` ورجعت `3.10$`، بعد إعادة التشغيل تصير قمتها
`3.10$`، فأرضية الارتداد تنزل من `4.25$` إلى `2.64$` — يعني تسليم `1.61$` زيادة
بصمت. وأسوأ منها: `soft_protect` يتجاهل أي صفقة قمتها ما وصلت عتبة التأمين، فصفقة
ربحت `5$` قبل إعادة التشغيل تفقد حارسها بالكامل.

صارت القمم تُحفظ بملف الحالة، تُكتب كل عشرين ثانية، وفوراً إذا انغلقت صفقة حتى ما
يرث تذكرتها رقمٌ جديد.
