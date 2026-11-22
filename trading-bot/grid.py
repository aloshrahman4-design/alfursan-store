"""Two-sided stop-order grid with a capped lot ladder.

The shape comes from the martingale grids sold on social media: a ladder of pending stop
orders above and below price, each step a fixed distance apart, the size doubling every
couple of levels so that whichever way price runs, the later big lots pay back the earlier
small ones.

What is different here is the cap. Uncapped, that ladder is arithmetic suicide: twenty
levels from a 0.01 base reaches 5.12 lots, needs $16,777 of margin, and a price round trip
through the whole grid loses $71,690. Cap the lot at 0.03 and the same twenty levels need
$443 of margin and lose $1,234 — the grid still works, the explosion is gone. The cap is
the entire design; everything else is bookkeeping.

Two hard limits sit on top, because a grid's danger is never one trade, it is the sum:
GRID_TP_CASH closes the whole basket on profit, GRID_MAX_LOSS closes it on loss and then
refuses to re-arm. The second one is the account saver and it does not restart by itself.
"""

import asyncio
import entry_safety as safety   # whose trade is it, and may we place this?
from entry_safety import require_entry_permission
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
import time

log = logging.getLogger("harvester.grid")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "grid_state.json")

# Its own magic numbers: the anchor strategy and the grid must never see each other's
# trades, or each would "harvest" positions the other is relying on.
MAGIC_GRID_BUY, MAGIC_GRID_SELL = 777333, 777444
MAGICS = (MAGIC_GRID_BUY, MAGIC_GRID_SELL)


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


GRID_ENABLED = os.environ.get("GRID_ENABLED", "0") in ("1", "true", "yes")
# The official base. It was 0.01 while the grid was being proved out; the owner moved it
# to 0.02 once the ceilings (position cap, basket stop, per-trade break-even) had all been
# shown to hold on the live account. Raising the base is the one change here that scales
# every risk figure linearly, so it belongs in the code where it is read, not only in a
# .env nobody re-reads: with GRID_MAX_LOT at 0.02 the ladder is a flat 0.02, and the
# margin an 8-level ladder reserves goes from about $193 to about $257 at a $4,000 gold.
GRID_BASE_LOT = _f("GRID_BASE_LOT", 0.02)
GRID_MAX_LOT = _f("GRID_MAX_LOT", 0.03)       # the cap that makes the whole thing viable
GRID_LEVELS = _i("GRID_LEVELS", 12)           # per side
GRID_STEP = _f("GRID_STEP", 2.0)              # price distance between levels
GRID_DOUBLE_EVERY = max(1, _i("GRID_DOUBLE_EVERY", 2))
GRID_CONTRACT = _f("GRID_CONTRACT", 100.0)    # units per lot: gold is 100 oz
GRID_TP_CASH = _f("GRID_TP_CASH", 5.0)        # the basket is not taken below this
# A take profit carried by each order itself, in dollars. The basket rule waits for the
# whole net to clear its target, so a level that ran into profit while the other side was
# bleeding was never taken - price reversed and a real gain turned into a real loss. This
# puts the exit at the broker: each level closes at its own +X$ immediately, even if the
# bot is offline or the move is too fast to poll. 0 disables it and leaves only the basket.
# Off by default now: closing at a fixed +$2 capped every winner at exactly $2. The $2 is
# a floor the trailing stop defends instead, so a trade that keeps running keeps earning.
GRID_TP_PER_TRADE = _f("GRID_TP_PER_TRADE", 0.0)
# Once past GRID_TP_CASH the basket is allowed to run, and is taken when it gives back this
# share of its own peak - the same rule the anchor strategy uses, for the same reason: a
# flat take closes a strong move at exactly the target and leaves the rest on the table.
GRID_TRAIL_PCT = _f("GRID_TRAIL_PCT", 10.0)   # 0 = take the moment it reaches the target
# A percentage give-back is meaningless on a small basket: 10% of a $3 peak is $0.30, well
# inside one tick's worth of noise, so the trail would fire immediately and behave exactly
# like the flat take it replaced. The give-back is never smaller than this.
GRID_TRAIL_MIN = _f("GRID_TRAIL_MIN", 1.0)
GRID_MAX_LOSS = _f("GRID_MAX_LOSS", 0.0)      # 0 = work it out from the percentage
GRID_MAX_LOSS_PCT = _f("GRID_MAX_LOSS_PCT", 15.0)
GRID_MARGIN_CEILING_PCT = _f("GRID_MARGIN_CEILING_PCT", 40.0)
# The loss limit is a share of the balance, and balance barely moves. Asking the broker for
# it on every tick was a third of the request load on its own, which slowed the price reads
# enough that the connection kept being declared stale and rebuilt.
GRID_BALANCE_TTL = _f("GRID_BALANCE_TTL", 30.0)
# How long a tick waits when every read is billed by the broker's API.
TICK_SECS_METERED = _f("TICK_SECS_METERED", 15.0)
# Set by the main module to a price source that does not cost an API read. The engines
# used to ask the broker for a price on every tick, which is a metered call for a number
# that is free from three public feeds and already calibrated against this broker.
PRICE_HOOK = None


async def quote_now(conn, symbol):
    """The tick's price: free feed when one is wired up, the broker otherwise."""
    if PRICE_HOOK is not None:
        try:
            q = await PRICE_HOOK(symbol)
            if q and q.get("bid") and q.get("ask"):
                return q
        except Exception as e:
            log.warning(f"free price failed, asking the broker: {e}")
    return await asyncio.wait_for(conn.get_symbol_price(symbol=symbol), timeout=6.0)
# When the broker answers "you have spent your quota, wait", the worst thing to do is
# keep asking: every refused call still counts against the window it is waiting to clear.
# So a quota refusal puts all reads to sleep for a while instead of retrying at tick rate.
QUOTA_COOLDOWN_SECS = _f("QUOTA_COOLDOWN_SECS", 300.0)
_quota = {"until": 0.0, "hits": 0, "last": ""}
# A pending order at the broker fills BY ITSELF, with no request from us. Managing what
# it opens needs reads - price, positions - and those are exactly what a quota refusal
# blocks. So a blind bot is a one-way valve: trades keep opening and nothing ever closes.
# If we cannot see for this long, we pull every order we placed. Cancelling is a trade
# operation on its own budget, and the ids come from our own record, so it needs no read.
BLIND_SECS = _f("BLIND_SECS", 120.0)
_reads = {"ok": 0.0}
_blind = {"cut": False}
_brake_said = 0.0
_stranded_said = 0.0   # last "winner I cannot close" warning
_paused_said = 0.0     # last 'grid on but system paused' notice
_halt_said = 0.0       # last 'the grid is halted' notice
_armfail_said = 0.0    # last 'could not arm, still trying' notice
# How many refusals in a row before the grid really stops. One was far too few: a
# single bad setting latched a permanent halt that outlived every fix for it.
ARM_FAIL_HALT = max(1, _i("ARM_FAIL_HALT", 10))
ARM_RETRY_SECS = _f("ARM_RETRY_SECS", 30.0)


# How old the last successful broker read may be before the bot refuses to OPEN
# anything. Generous on purpose: this is about a dead connection, not a slow tick.
ENTRY_MAX_BLIND_SECS = _f("ENTRY_MAX_BLIND_SECS", 180.0)


def note_read_ok():
    _reads["ok"] = time.time()
    _blind["cut"] = False


def blind_for():
    """Seconds since the last read that actually worked."""
    if not _reads["ok"]:
        _reads["ok"] = time.time()
    return max(0.0, time.time() - _reads["ok"])


def remember_order(order_id):
    if not order_id:
        return
    st = load()
    ids = st.setdefault("pending_ids", [])
    if order_id not in ids:
        ids.append(order_id)
        save(st)


def forget_orders():
    st = load()
    if st.get("pending_ids"):
        st["pending_ids"] = []
        save(st)


async def cancel_known(conn, notify=None):
    """Cancel every order we recorded placing. Uses no read, so it works while blind."""
    ids = list(load().get("pending_ids", []))
    if not ids:
        return 0
    killed = 0
    for oid in ids:
        try:
            await asyncio.wait_for(conn.cancel_order(oid), timeout=8.0)
            killed += 1
        except Exception as e:
            # Already filled or already gone is the common case and not a problem.
            log.info(f"blind cancel of {oid}: {e}")
    forget_orders()
    if notify:
        await notify(
            f"🕶 *ما أكدر أشوف السوق — سحبت أوامري*\n"
            f"• (لو كان وقف السيرفر مفعّل ما كنت سحبتها — الصفقة تولد محمية)\n"
            f"• صار `{blind_for()/60:.0f}` دقيقة وأنا ما أقدر أقرأ السعر ولا الصفقات\n"
            f"• الأمر المعلّق ينفتح لحاله عند الوسيط حتى لو أنا أعمى — "
            f"يعني تنفتح صفقات وما أكدر أتابعها ولا أغلقها\n"
            f"• فألغيت `{killed}` أمر من `{len(ids)}` حتى ما يزيد تعرّضك\n"
            f"• الصفقات المفتوحة ما لمستها — هي عند الوسيط بوقفها\n"
            f"• أرجع أنصب لحالي أول ما أرجع أشوف")
    return killed


def is_quota_error(err):
    text = str(err or "").lower()
    return ("cpu credits" in text or "rate limit" in text
            or "too many requests" in text or "429" in text)


def note_api_error(err):
    """Returns True when this was the broker refusing on quota, and starts the wait."""
    if not is_quota_error(err):
        return False
    _quota["until"] = time.time() + QUOTA_COOLDOWN_SECS
    _quota["hits"] += 1
    _quota["last"] = str(err)[:200]
    log.warning(f"quota refusal #{_quota['hits']}: holding off reads for "
                f"{QUOTA_COOLDOWN_SECS:.0f}s")
    return True


def cooling_down():
    """Seconds still to wait before it is worth asking the broker anything."""
    return max(0.0, _quota["until"] - time.time())


def quota_note():
    if not _quota["hits"]:
        return ""
    left = cooling_down()
    if left:
        return (f"⏸ *متوقف مؤقتاً عن الاستعلام* — الوسيط رفض `{_quota['hits']}` مرة "
                f"لتجاوز الحصة. أعيد المحاولة بعد `{left/60:.0f}` دقيقة.")
    return f"• تجاوزنا الحصة `{_quota['hits']}` مرة من آخر تشغيل — الاستعلام شغال الآن."


def quota_clear():
    _quota.update({"until": 0.0, "hits": 0, "last": ""})


def reads_free(conn=None):
    """True when positions and prices come from a local cache rather than the API."""
    return bool(getattr(conn if conn is not None else _last_conn, "streaming", False))


_last_conn = None
# Levels that fill and close leave holes. The grid only re-arms once every position AND
# every order is gone, so price can end up sitting in an $18 dead zone - nearest buy stop
# far above, nearest sell stop far below - doing nothing for hours. Top the ladder back up
# near price instead: it keeps the grid working without disturbing what is already open.
GRID_REFILL = os.environ.get("GRID_REFILL", "1") not in ("0", "false", "no")
GRID_GAP_FACTOR = _f("GRID_GAP_FACTOR", 1.5)   # a hole this many steps wide is refilled
# How far price may drift from the centre before the ladder is laid again. Re-centring
# used to need the book COMPLETELY empty - no positions and no orders. So the ordinary
# case went unhandled: targets close the winners, price walks away, and what is left is
# a handful of pendings stranded behind the market with nothing near it. refill_gaps
# patches one hole per side per tick, which is a repair, not a re-centre. The grid sat
# there looking dead, and it was: nothing it held could be reached any more.
GRID_RECENTER_STEPS = _f("GRID_RECENTER_STEPS", 3.0)
# Break-even. A trade that climbed part of the way to its target and then turned around
# used to hand the whole gain back and keep going into loss. Once it has shown BE_TRIGGER
# of profit its stop moves to entry, so from then on it either reaches the target or closes
# flat. This only ever protects a gain - it can never force a loss - and the lock covers
# the spread, so "flat" means flat after costs rather than minus the spread.
BE_ENABLED = os.environ.get("BE_ENABLED", "1") not in ("0", "false", "no")
BE_TRIGGER_CASH = _f("BE_TRIGGER_CASH", 0.35)
BE_LOCK_CASH = _f("BE_LOCK_CASH", 0.10)
# Break-even alone saves the trade and loses the gain: a position that reached +$1.80 and
# turned around closed at zero, with the 1.80 gone. Once secured, the stop keeps following
# the peak this far behind it, so a reversal banks most of what the move actually gave.
BE_TRAIL_CASH = _f("BE_TRAIL_CASH", 0.50)
# Once a trade has shown this much, the stop never gives it back. Below it the trail
# governs; at and above it the floor does, so +$2 becomes a level the trade defends
# rather than the point it is taken at.
BE_FLOOR_CASH = _f("BE_FLOOR_CASH", 2.0)
# A cash trail converts to wildly different price distances by lot size: $0.50 is $0.50 of
# price on 0.01 lot and $0.10 on 0.05 - tighter than the spread, so the stop landed above
# the market and was refused, leaving the big trades with no protection at all. The stop is
# pulled back to a placeable distance instead of skipped, as long as it still locks a gain.
# This is a SAFETY MARGIN on top of whatever the broker demands, not the demand itself.
# It used to be max(spread, BE_MIN_GAP) - the spread standing in for the broker's minimum
# stop distance, which it is not: the spread is buy-minus-sell, and has nothing to do with
# how close a stop may sit to the market. Using it cost 0.32$ of price on gold, so on a
# 0.02 lot no stop could be placed until the trade was 1.28$ up, while BE_TRIGGER_CASH
# claimed it was protected from 0.35$. The broker's real number is asked for below.
BE_MIN_GAP = _f("BE_MIN_GAP", 0.10)
# The window the broker cannot cover. Below `spread + gap` above entry, any stop would sit
# under break-even and be a LOSING stop, so none is placed and the trade is bare. In that
# window only the bot itself can act: if a trade that showed BE_TRIGGER_CASH gives it all
# back, close it at about break-even rather than let it turn red.
#
# OFF by default. It was my idea, not the owner's, and he turned it down once he saw it:
# he would rather a trade keep running than be shut at zero on a wobble. That is a sound
# preference and it is his to make - the thing CLOSES TRADES, and a feature that closes
# trades has no business being on by default because someone thought it was clever.
# What he asked for is still exactly what runs: a stop at entry the moment the trade is
# in profit, then the trail, then the $2 floor. Nothing in that path changes here.
BE_SOFT = os.environ.get("BE_SOFT", "0") not in ("0", "false", "no")
BE_SOFT_EXIT = _f("BE_SOFT_EXIT", 0.05)
# Hard ceilings on what the grid may be holding at once. The level cap only ever limited
# PENDING orders per side; nothing limited open positions, so refilling and recycling kept
# feeding a trend until twenty were open together and the basket stop took them all at once.
GRID_MAX_POSITIONS = _i("GRID_MAX_POSITIONS", 8)
# And a soft ceiling in money: stop adding once the basket is most of the way to its stop.
GRID_ADD_STOP_PCT = _f("GRID_ADD_STOP_PCT", 50.0)
# GRID_ADD_STOP_PCT is a percentage OF GRID_MAX_LOSS, so setting the basket stop to
# 99999 to take manual control silently disabled the expansion brake with it: 50% of
# 99999 is a number no drawdown reaches. The brake that matters is measured against the
# ACCOUNT, not against another setting, and nothing the user changes can switch it off.
# Floating loss this far below balance and the grid adds nothing, arms nothing, and
# pulls what is already armed.
GRID_EQUITY_BRAKE_PCT = _f("GRID_EQUITY_BRAKE_PCT", 10.0)
# --- the trend guard -----------------------------------------------------------------
# A grid earns in a range and bleeds in a trend: price walks one way, every level it
# crosses opens a trade that is immediately under water, and refill keeps putting new
# ones in its path. That is what the -$101.65 and the -$214 both were.
#
# The old GRID_ONE_WAY tried to fix it and got the sign backwards. It read the committed
# direction off WHICH SIDE HELD POSITIONS - but in a grid the winners close and the
# LOSERS are what stays open, so it named the losing side as "committed" and cancelled
# the winning one. Three losing sells ended up with the buy ladder pulled while price
# climbed $15 away from them. It has been deleted, not left switched off.
#
# This reads the direction off PRICE against the grid's own centre, which cannot invert:
# price `GRID_TREND_STEPS` steps above the centre means the SELL ladder is the one being
# run over, so the SELL side stands down and the BUY side keeps following price up.
GRID_TREND = os.environ.get("GRID_TREND", "1") not in ("0", "false", "no")
# A standing direction the user sets by hand, which the trend guard cannot give him.
# The guard is a REACTION: after every re-arm it starts from a clean slate and needs
# price to travel GRID_TREND_STEPS from the NEW centre and hold there, so in a market
# that has been falling for a day the grid still lays a buy ladder each cycle and still
# feeds it until the guard catches up. "both" keeps the old behaviour; "down" arms and
# refills only the sell side; "up" only the buy side. One side also halves the margin
# the ladder reserves, which is what lets a blocked grid arm at all.
_BIAS_WORDS = {"both": "both", "all": "both", "2": "both", "الاثنين": "both", "كلها": "both",
               "down": "down", "sell": "down", "short": "down", "هبوط": "down", "بيع": "down",
               "up": "up", "buy": "up", "long": "up", "صعود": "up", "شراء": "up"}
GRID_BIAS = _BIAS_WORDS.get(os.environ.get("GRID_BIAS", "both").strip().lower(), "both")
# How far past the centre counts as a trend rather than a wiggle, in grid steps.
GRID_TREND_STEPS = _f("GRID_TREND_STEPS", 3.0)
# ...and it has to hold there this long. A single spike through three levels and straight
# back is the exact move a grid is built to earn from; standing a side down for it would
# throw away the trade instead of taking it.
GRID_TREND_HOLD = _f("GRID_TREND_HOLD", 60.0)
# Hysteresis: once a trend is called it stays called until price comes back inside this
# many steps of the centre. Without the gap, one candle either side of the threshold
# cancels and re-places the same ladder every minute.
GRID_TREND_RELEASE = _f("GRID_TREND_RELEASE", 1.5)

# --- staged halving of a trade price has run away from -------------------------------
# The owner's rule, and the mirror image of the martingale: when a trade is proved wrong,
# make it SMALLER. Every HALF_DISTANCE dollars that price travels against the entry, half
# the remaining volume is closed. What is left keeps its own break-even and trail.
#
# It is deliberately NOT funded by grid profit the way the cleanup cut is. This is risk
# control, and it is needed exactly when the account is losing - tying it to a profit
# balance would switch it off at the moment it matters.
#
# On a 0.02 ladder this fires once (0.02 -> 0.01) because 0.01 is the broker's minimum
# and cannot be split. On a bigger lot it stages: 0.04 at $5, again at $10.
HALF_ENABLED = os.environ.get("HALF_ENABLED", "1") not in ("0", "false", "no")
HALF_DISTANCE = _f("HALF_DISTANCE", 5.0)      # dollars of adverse travel per halving


def half_distance(price=0.0):
    """How far price must run against a trade before half of it is cut.

    HALF_DISTANCE is a PRICE distance, and $5 is a considered number on gold near
    $4,000 - a tenth of a percent. The same $5 on bitcoin near $66,000 is 0.008%,
    which the market crosses in seconds: every losing trade would be halved almost
    at once, repeatedly, until it hit the minimum lot. The setting keeps its meaning
    by holding its RATIO to price, exactly as the grid step does.
    """
    if not price or HALF_DISTANCE <= 0:
        return HALF_DISTANCE
    gold_ratio = HALF_DISTANCE / 4000.0            # what the number meant when chosen
    return max(HALF_DISTANCE, round(float(price) * gold_ratio, 2))
HALF_MIN_LOT = _f("HALF_MIN_LOT", 0.01)       # the broker's smallest tradable volume

# --- bounded recovery ----------------------------------------------------------------
# The one idea worth taking from the hedge-recovery bots, with the ruin removed: after a
# run of losing cycles the next ladder starts one notch bigger, so a single good cycle
# recovers more of the hole. Theirs doubles with no end - the video's own ladder reached
# 20.48 lots and -$1,770 floating. This one steps by a flat GRID_RECOVER_STEP and stops
# dead at the cap: 0.01, then 0.02, then 0.03, then 0.03 for ever.
# Off by default. It is the only setting here that RAISES risk, so it is the user's call,
# not a default - `/setkey recover 1` turns it on.
GRID_RECOVER = os.environ.get("GRID_RECOVER", "0") not in ("0", "false", "no")
GRID_RECOVER_AFTER = max(1, _i("GRID_RECOVER_AFTER", 3))   # losing cycles per notch
GRID_RECOVER_STEP = _f("GRID_RECOVER_STEP", 0.01)          # added per notch, never doubled
GRID_RECOVER_MAX = _f("GRID_RECOVER_MAX", 0.03)            # and never past GRID_MAX_LOT
# Harvest each side on its own merits instead of waiting for the two to net out, and hand
# the other side's open trades to the user rather than closing them with it. He asked for
# that split explicitly: the bot takes the winners, he decides what to do with the losers.
# It is the one arrangement where releasing losers is not the usual grid suicide - they
# stop being the bot's problem entirely, so nothing accumulates behind its back.
GRID_SIDE_HARVEST = os.environ.get("GRID_SIDE_HARVEST", "1") not in ("0", "false", "no")
# Releasing a trade gives up the decision, not the watch. A trade nobody is looking at is
# how a $20 loss quietly becomes a $400 one, so the bot keeps reporting on what it handed
# over: the moment it turns green (the user's chance to take it), every step deeper, and a
# standing summary while any of them is still open.
GRID_RELEASED_STEP = _f("GRID_RELEASED_STEP", 25.0)
GRID_RELEASED_SUMMARY_MIN = _f("GRID_RELEASED_SUMMARY_MIN", 30.0)
GRID_RELEASE_LOSERS = os.environ.get("GRID_RELEASE_LOSERS", "1") not in ("0", "false", "no")


def ladder(base=None, cap=None, levels=None, double_every=None):
    """The lot for each level: doubling, but never past the cap."""
    base = GRID_BASE_LOT if base is None else base
    cap = GRID_MAX_LOT if cap is None else cap
    levels = GRID_LEVELS if levels is None else levels
    double_every = GRID_DOUBLE_EVERY if double_every is None else max(1, double_every)
    return [round(min(base * 2 ** (i // double_every), cap), 2) for i in range(max(0, levels))]


def tp_distance(volume, cash=None, contract=None):
    """Price move that turns into `cash` on this volume. 0.01 lot of gold: $1 = $1."""
    cash = GRID_TP_PER_TRADE if cash is None else cash
    contract = GRID_CONTRACT if contract is None else contract
    per_dollar = float(volume) * contract
    if cash <= 0 or per_dollar <= 0:
        return None
    return round(cash / per_dollar, 2)


def worst_case(lots=None, step=None, contract=None):
    """Loss if every level on one side fills and price then returns to the centre.

    This is the number that decides whether a grid is survivable, and it is the one the
    adverts never show.
    """
    lots = ladder() if lots is None else lots
    step = GRID_STEP if step is None else step
    contract = GRID_CONTRACT if contract is None else contract
    return round(sum((i + 1) * step * contract * lot for i, lot in enumerate(lots)), 2)


def best_case(lots=None, step=None, contract=None):
    """Gain if every level fills and price keeps going to the end of the grid."""
    lots = ladder() if lots is None else lots
    step = GRID_STEP if step is None else step
    contract = GRID_CONTRACT if contract is None else contract
    n = len(lots)
    # Level i fills at (i+1) steps out, so reaching the end of the grid leaves it
    # (n - i - 1) steps in profit — the last level to fill makes nothing at all.
    return round(sum((n - i - 1) * step * contract * lot for i, lot in enumerate(lots)), 2)


def whipsaw_case(lots=None, step=None, contract=None):
    """Loss if price sweeps one side, comes back, and keeps going through the other.

    The real worst case, and much worse than a one-sided round trip: every level on the
    first side is now a full grid-width under water while the second side has only just
    begun to earn. This is the number GRID_MAX_LOSS exists to cut short.
    """
    lots = ladder() if lots is None else lots
    step = GRID_STEP if step is None else step
    contract = GRID_CONTRACT if contract is None else contract
    n = len(lots)
    losing = sum(((i + 1) * step + n * step) * contract * lot for i, lot in enumerate(lots))
    winning = sum((n * step - (j + 1) * step) * contract * lot for j, lot in enumerate(lots))
    return round(losing - winning, 2)


def bias_allows(magic):
    """Whether the standing bias lets this side be armed or refilled at all."""
    if GRID_BIAS == "down":
        return magic == MAGIC_GRID_SELL
    if GRID_BIAS == "up":
        return magic == MAGIC_GRID_BUY
    return True


def bias_sides():
    """How many sides a ladder will occupy - the margin it reserves scales with this."""
    return 2 if GRID_BIAS == "both" else 1


def bias_text():
    return {"down": "هبوط فقط (بيع) 🔻", "up": "صعود فقط (شراء) 🔺"}.get(
        GRID_BIAS, "الاثنين ↔️")


def exposure_lots(lots=None):
    """The lots that can actually be open at the same time.

    The ladder is GRID_LEVELS deep per side, but GRID_MAX_POSITIONS now caps open
    positions for real (the engine pulls the remaining pendings when it is reached), so
    quoting a full twelve-level round trip overstated every risk figure the user reads.
    """
    lots = ladder() if lots is None else lots
    return lots[:GRID_MAX_POSITIONS] if GRID_MAX_POSITIONS > 0 else lots


def recovery_base(streak=0):
    """Base lot for the next ladder, stepped up after a run of losing cycles.

    Arithmetic and capped, which is the whole difference from a martingale: it adds
    GRID_RECOVER_STEP per notch and can never exceed GRID_MAX_LOT, so the worst case is
    the same worst case as always - unlike doubling, where the worst case has no bound.
    """
    base = round(min(GRID_BASE_LOT, GRID_MAX_LOT), 2)
    try:
        streak = int(streak or 0)
    except (TypeError, ValueError):
        return base
    if not GRID_RECOVER or streak < GRID_RECOVER_AFTER:
        return base
    cap = round(min(GRID_RECOVER_MAX, GRID_MAX_LOT), 2)
    return round(min(base + GRID_RECOVER_STEP * (streak // GRID_RECOVER_AFTER), cap), 2)


# Direction memory. `dir` is the confirmed trend, `pending` one that has not held long
# enough yet, `down` the magic currently stood down. In memory on purpose: a restart
# should re-prove the trend rather than inherit a stale one.
_trend = {"dir": 0, "pending": 0, "since": 0.0, "down": None}


def trend_reset():
    _trend.update({"dir": 0, "pending": 0, "since": 0.0, "down": None})


def trend_read(center, mid, now=None):
    """+1 if price has committed above the grid's centre, -1 below, 0 inside the band.

    Measured off price, never off which side holds positions - that is the inversion
    that broke the old one-way rule.
    """
    now = time.time() if now is None else now
    try:
        center, mid = float(center or 0), float(mid or 0)
    except (TypeError, ValueError):
        return 0
    if not center or not mid or GRID_STEP <= 0:
        return 0
    steps = (mid - center) / GRID_STEP
    cur = _trend["dir"]
    if cur:
        # Already in a trend: it holds until price comes back inside the release band.
        if (cur > 0 and steps <= GRID_TREND_RELEASE) or (cur < 0 and steps >= -GRID_TREND_RELEASE):
            _trend.update({"dir": 0, "pending": 0, "since": 0.0})
            return 0
        return cur
    want = 1 if steps >= GRID_TREND_STEPS else (-1 if steps <= -GRID_TREND_STEPS else 0)
    if not want:
        _trend.update({"pending": 0, "since": 0.0})
        return 0
    if _trend["pending"] != want:
        _trend.update({"pending": want, "since": now})
        return 0
    if now - _trend["since"] < GRID_TREND_HOLD:
        return 0                          # proving itself; not a trend yet
    _trend.update({"dir": want, "pending": 0, "since": now})
    return want


def trend_stand_down(direction):
    """The side a confirmed trend is running over - the one that must stop being fed.

    Price above the centre means the SELL ladder is the one underwater, so SELL stands
    down and BUY keeps following price. The old rule did exactly the opposite.
    """
    if direction > 0:
        return MAGIC_GRID_SELL
    if direction < 0:
        return MAGIC_GRID_BUY
    return None


_bias_warned = 0        # the direction last warned about, so it is said once per turn


def bias_reset():
    global _bias_warned
    _bias_warned = 0


def notices_reset():
    """Un-mute the 'why I am not arming' notices.

    They are rate-limited so a stuck grid does not repeat itself every tick. But the
    limit also silenced the answer to a command the USER had just typed: /grid on at
    2:51 decided not to arm and said nothing, because the same notice had gone out at
    2:39. From his side the bot simply ignored him. Anything he types resets this.
    """
    global _brake_said, _blocked_at, _stranded_said, _paused_said, _halt_said
    global _armfail_said
    _brake_said = 0.0
    _blocked_at = 0.0
    _stranded_said = 0.0
    _paused_said = 0.0
    _halt_said = 0.0
    _armfail_said = 0.0


async def bias_watch(notify=None):
    """Warn when the measured trend has turned against the direction the user pinned.

    A standing bias is a judgement about the market, and markets change. Pinning "sells
    only" during a two-day slide is sound; leaving it pinned through the reversal is how
    a good call becomes a bad position. The bot cannot unpin it - that is the user's
    decision - but going quiet about it would be the worst of both.
    """
    global _bias_warned
    if GRID_BIAS == "both":
        return False
    direction = _trend["dir"]
    against = (GRID_BIAS == "down" and direction > 0) or (GRID_BIAS == "up" and direction < 0)
    if not against:
        if _bias_warned and direction == 0:
            _bias_warned = 0          # back inside the band: the warning re-arms
        return False
    if _bias_warned == direction:
        return False                  # already said, and nothing has changed
    _bias_warned = direction
    if notify:
        way = "صاعد" if direction > 0 else "هابط"
        pinned = "هبوط فقط (بيع)" if GRID_BIAS == "down" else "صعود فقط (شراء)"
        fix = "/grid up" if direction > 0 else "/grid down"
        await notify(
            f"🔄 *انتبه — السوق انقلب*\n"
            f"• قياسي: الاتجاه صار *{way}* وثابت عليه\n"
            f"• وانت مثبّت الشبكة على *{pinned}*\n"
            f"• يعني صفقاتي الجديدة كلها بالجهة الخاسرة\n\n"
            f"ما أغيّر قرارك لحالي. لو تريد تمشي مع السوق: `{fix}` "
            f"أو `/grid both` للجهتين.")
    return True


async def trend_guard(conn, symbol, center, orders, notify=None, quote=None):
    """Stop feeding the side price is running away from; leave open trades alone.

    Returns the magic to keep stood down this tick, or None. It only ever cancels
    PENDING orders - what is already open keeps its own stop and its own exit.

    `quote` is the tick's already-fetched price. The engine reads the price once and
    hands the same one to the guard and to refill, because a second call per tick is
    how the grid produced eleven reconnects in twenty-two minutes the last time.
    """
    if not GRID_TREND:
        return None
    try:
        if quote is None:
            quote = await asyncio.wait_for(conn.get_symbol_price(symbol=symbol), timeout=6.0)
        bid, ask = float(quote.get("bid", 0) or 0), float(quote.get("ask", 0) or 0)
    except Exception as e:
        log.warning(f"trend guard has no price: {e}")
        return _trend["down"]             # keep the last decision rather than flip blind
    if not bid or not ask:
        return _trend["down"]
    direction = trend_read(center, (bid + ask) / 2.0)
    down = trend_stand_down(direction)
    if down == _trend["down"]:
        return down                       # nothing changed; stay quiet
    was, _trend["down"] = _trend["down"], down
    if down is None:
        if notify and was is not None:
            await notify("🧭 *رجع السعر لنطاق الشبكة* — رجّعت الجهة الموقوفة للعمل، "
                         "الجهتين شغالات من جديد.")
        return None
    stale = [o for o in orders if o.get("magic") == down]
    cancelled = 0
    for o in stale:
        try:
            await asyncio.wait_for(conn.cancel_order(o["id"]), timeout=6.0)
            cancelled += 1
        except Exception as e:
            log.warning(f"trend guard could not cancel {o.get('id')}: {e}")
    if notify:
        way = "فوق" if direction > 0 else "تحت"
        side_ar = "البيع" if down == MAGIC_GRID_SELL else "الشراء"
        other_ar = "الشراء" if down == MAGIC_GRID_SELL else "البيع"
        await notify(
            f"🧭 *السعر التزم اتجاه — مشى {way} أكثر من `{GRID_TREND_STEPS:g}` خطوات عن المركز*\n"
            f"• وقّفت جهة *{side_ar}* — هذي الجهة اللي السعر يدهسها، ما أضيف عليها بعد\n"
            f"• جهة *{other_ar}* تبقى شغالة وتمشي مع الحركة\n"
            f"• ألغيت `{cancelled}` أمر معلّق · الصفقات المفتوحة ما لمستها")
    return down


def loss_limit(balance=0.0):
    """The floating loss at which the basket is closed and the grid stands down."""
    if GRID_MAX_LOSS > 0:
        return GRID_MAX_LOSS
    return round(max(1.0, float(balance) * GRID_MAX_LOSS_PCT / 100.0), 2)


def _blank():
    return {"armed": False, "center": None, "armed_at": 0, "cycles": 0,
            "realized": 0.0, "halted": False, "halt_reason": "", "released": [],
            # The account's high-water mark. Deliberately NOT cleared by reset(): a
            # drawdown that survives a re-centre is the whole point of measuring it.
            "equity_hwm": 0.0,
            # Positions the owner handed TO the bot. The mirror image of "released":
            # trades it did not open, that he has asked it to protect anyway.
            "adopted": [],
            # Best profit each open position has shown. Persisted because three
            # protections measure from it and a restart used to zero all three.
            "peaks": {},
            "loss_streak": 0, "pending_ids": []}


def adopted_ids():
    """Positions the bot did not open but has been told to manage."""
    return set(load().get("adopted", []))


def adopt(pid):
    """Take over a trade opened by hand. Returns True if this changed anything.

    Deliberately explicit rather than automatic: protecting a trade means being
    allowed to CLOSE it, and the bot must never assume that permission over money
    somebody else put at risk.
    """
    pid = str(pid)
    state = load()
    ids = [str(i) for i in state.get("adopted", [])]
    # Adopting undoes a release, or the two lists would contradict each other and
    # whichever check ran last would decide - the kind of ambiguity that has already
    # cost this project a day. This runs BEFORE the "already adopted" exit: a trade
    # that is on both lists is exactly the case that needs repairing, and an early
    # return would leave it broken for good.
    rel = [i for i in state.get("released", []) if str(i) != pid]
    fixed = len(rel) != len(state.get("released", []))
    state["released"] = rel
    if pid in ids:
        if fixed:
            save(state)
        return fixed
    ids.append(pid)
    state["adopted"] = ids[-200:]
    save(state)
    return True


def unadopt(pid):
    """Hand a trade back. The bot stops touching it from the next tick."""
    pid = str(pid)
    state = load()
    ids = [str(i) for i in state.get("adopted", [])]
    if pid not in ids:
        return False
    state["adopted"] = [i for i in ids if i != pid]
    save(state)
    return True


def load():
    try:
        with open(STATE_PATH) as f:
            state = _blank()
            state.update(json.load(f))
            state.setdefault("released", [])
            state.setdefault("adopted", [])
            state.setdefault("peaks", {})
            state.setdefault("loss_streak", 0)
            state.setdefault("pending_ids", [])
            return state
    except Exception:
        return _blank()


def save(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.warning(f"could not save grid state: {e}")


def reset():
    """Clear the grid's own state, but never the released list.

    Those trades were handed to the user permanently. Wiping the list on a re-centre would
    quietly take them back: the bot would start counting them again, and the next teardown
    would close positions it had promised not to touch.
    """
    notices_reset()
    fresh = _blank()
    old = load()
    fresh["released"] = old.get("released", [])
    # The losing run survives a re-centre on purpose: recovery sizing is about the run of
    # cycles, and zeroing it here would make every re-arm look like a fresh start.
    fresh["loss_streak"] = old.get("loss_streak", 0)
    # The high-water mark survives too. A drawdown the user can erase by re-arming is
    # not a drawdown; it is a number that flatters whoever is looking at it.
    fresh["equity_hwm"] = old.get("equity_hwm", 0.0)
    # Adoptions survive for the same reason releases do: the owner asked for that
    # trade to be guarded, and a re-centre is not him changing his mind.
    fresh["adopted"] = old.get("adopted", [])
    # Peaks belong to positions that are still open; a re-centre does not close them.
    fresh["peaks"] = old.get("peaks", {})
    save(fresh)
    trend_reset()


async def book(conn, symbol=None, exclude=None):
    """One read, two answers: the grid's own trades, and the account's net direction.

    The net must count EVERY position on the symbol - anchor and handed-over trades
    included - because that is what decides whether a new order hedges or adds.
    """
    exclude = set(exclude if exclude is not None else load().get("released", []))
    pos = await asyncio.wait_for(conn.get_positions(), timeout=6.0)
    note_read_ok()
    mine = [p for p in pos if ours(p, exclude, symbol)]
    sym = symbol or GRID_SYMBOL
    elsewhere = round(sum(float(p.get("unrealizedProfit", 0.0) or 0.0)
                          for p in pos if p.get("symbol") != sym), 2)
    return mine, net_exposure(pos, symbol), elsewhere


def ours(p, exclude, symbol=None):
    """A grid trade of ours, ON THE INSTRUMENT WE ARE TRADING.

    The symbol test was missing everywhere, and it only became visible on the day the
    bot changed instrument. Switching to bitcoin left four gold grid trades matching
    the magic numbers, so they still filled the position cap, still counted inside the
    basket's profit (which is why a basket $2000 under water could never be harvested),
    still drove the basket stop - and /grid off would have closed them. None of that
    is bitcoin's business, and none of it was visible on any screen.
    """
    return (p.get("magic") in MAGICS
            and p.get("symbol") == (symbol or GRID_SYMBOL)
            and p.get("id") not in exclude)


async def grid_positions(conn, exclude=None, symbol=None):
    """Grid trades the bot still owns. Released ones are the user's and stay invisible."""
    exclude = set(exclude if exclude is not None else load().get("released", []))
    pos = await asyncio.wait_for(conn.get_positions(), timeout=6.0)
    note_read_ok()
    return [p for p in pos if ours(p, exclude, symbol)]


async def close_positions(conn, positions):
    """Close exactly these. Returns (count, realized)."""
    closed, booked = 0, 0.0
    for p in positions:
        try:
            booked += float(p.get("unrealizedProfit", 0.0) or 0.0)
            await asyncio.wait_for(conn.close_position(p["id"]), timeout=6.0)
            closed += 1
        except Exception as e:
            log.warning(f"could not close {p.get('id')}: {e}")
    return closed, round(booked, 2)


# His own idea, from earlier: a trade that gets stranded is not cut at a loss and not
# left to rot - it is PAID OFF out of what the grid has already banked. The fund is
# simply `realized`, the grid's net booked profit, so nothing needs a second ledger:
# closing the loser books it, which draws the fund down by exactly what it cost.
RESCUE_ENABLED = os.environ.get("RESCUE_ENABLED", "0") not in ("0", "false", "no")
RESCUE_STEPS = _f("RESCUE_STEPS", 3.0)      # how far price must have run against it
RESCUE_RESERVE = _f("RESCUE_RESERVE", 0.0)  # profit kept back, never spent on rescues


def rescue_fund(state=None):
    """What the grid can afford to spend clearing a stranded trade."""
    st = state if state is not None else load()
    return round(float(st.get("realized", 0.0) or 0.0) - RESCUE_RESERVE, 2)


def stranded(positions, bid, ask):
    """Losing grid trades price has walked away from, worst loss first."""
    if RESCUE_STEPS <= 0 or GRID_STEP <= 0:
        return []
    out = []
    for p in positions:
        try:
            loss = float(p.get("unrealizedProfit", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if loss >= 0:
            continue
        if adverse_distance(p, bid, ask) < RESCUE_STEPS * GRID_STEP:
            continue                 # still near price; it may yet come back on its own
        out.append((p, round(-loss, 2)))
    return sorted(out, key=lambda x: -x[1])


async def rescue_stranded(conn, positions, bid, ask, notify=None, allowed=True):
    """Spend banked profit to close the biggest stranded loser it can fully cover.

    One per call. Clearing the largest affordable loss removes the most drag, and
    booking it draws the fund down by that exact amount, so the next call sees the
    truth without anyone tracking a balance by hand.
    """
    if not (allowed and RESCUE_ENABLED) or not hasattr(conn, "close_position"):
        return 0
    st = load()
    fund = rescue_fund(st)
    if fund <= 0:
        return 0
    afford = [(p, cost) for p, cost in stranded(positions, bid, ask) if cost <= fund]
    if not afford:
        return 0
    p, cost = afford[0]
    try:
        await asyncio.wait_for(conn.close_position(p["id"]), timeout=8.0)
    except Exception as e:
        log.warning(f"rescue close refused on {p.get('id')}: {e}")
        return 0
    st = load()
    st["realized"] = round(float(st.get("realized", 0.0) or 0.0) - cost, 2)
    st["rescued"] = int(st.get("rescued", 0)) + 1
    save(st)
    if notify:
        await notify(
            f"🧹 *صفّيت صفقة عالقة من أرباح الشبكة*\n"
            f"• خسارتها `-{cost:g}$` دفعتها من المحصّل\n"
            f"• المحصّل كان `{fund + RESCUE_RESERVE:g}$` وصار "
            f"`{st['realized']:g}$`\n"
            f"• صافي الأثر عليك: صفر — بس انفك مستوى ورجع هامشك")
    return 1


def release(positions):
    """Hand these trades to the user: the bot stops counting, managing and closing them."""
    if not positions:
        return 0
    state = load()
    kept = set(state.get("released", [])) | {p.get("id") for p in positions}
    # ids of trades that closed long ago would pile up forever; the list stays bounded
    state["released"] = [i for i in kept if i][-200:]
    save(state)
    return len(positions)


def pnl(positions):
    return round(sum(float(p.get("unrealizedProfit", 0.0) or 0.0) for p in positions), 2)


async def clear_loss_stops(conn, positions, bid, ask, notify=None):
    """Remove loss-side stops from grid trades, leaving any profit trail untouched.

    MT5 clears a stop when it is set to 0, and the SDK forwards a numeric 0 as
    stopLoss while dropping None - so 0 is the clear and None is "leave it".
    """
    cleared = 0
    for p in positions:
        if p.get("magic") not in (MAGIC_GRID_BUY, MAGIC_GRID_SELL):
            continue
        sl = p.get("stopLoss")
        entry = float(p.get("openPrice", 0) or 0)
        if not sl or not entry:
            continue
        is_buy = p.get("type") == "POSITION_TYPE_BUY"
        pad = max(0.0, float(ask) - float(bid))
        # Worse than entry by more than the spread => a loss stop, not a trail.
        if not ((is_buy and float(sl) < entry - pad) or
                (not is_buy and float(sl) > entry + pad)):
            continue
        try:
            await asyncio.wait_for(conn.modify_position(p["id"], stop_loss=0), timeout=8.0)
            cleared += 1
        except Exception as e:
            log.warning(f"could not clear loss stop on {p.get('id')}: {e}")
    if cleared and notify:
        await notify(f"🧹 *شلت وقف الخسارة عن `{cleared}` صفقة* — بقى ستوب متابعة "
                     f"الربح بس")
    return 0


async def ensure_entry_stops(conn, positions, bid, ask, notify=None):
    """Put the hard stop on open trades that do not have one - where it is still safe.

    Writing it only onto new orders left every trade opened before the setting with no
    stop at all, which is what the owner saw: price reversed a dollar on a sell and
    nothing happened, because that sell predated the rule.

    The one case this must never do is apply a $1 stop to a trade that is already $20
    the wrong way: that is not a stop, it is an instant close at the worst price. So a
    position whose current price has already passed where the stop would sit is left
    alone and reported, not silently shut.

    Setting GRID_STOP_DIST to 0 means "no hard stop, only the profit trail" - and that
    has to UNDO the stops already written, not just stop writing new ones. A broker
    keeps a stop until something clears it, so turning the setting off while old stops
    sat on live trades would have left them armed and the owner believing they were
    gone. Clearing is narrow on purpose: only a stop sitting on the LOSING side of the
    entry is removed. A profit trail never sits there - it only exists at or past
    break-even - so this can never eat the protection he wants to keep.
    """
    if not hasattr(conn, "modify_position"):
        return 0
    if GRID_STOP_DIST <= 0:
        return await clear_loss_stops(conn, positions, bid, ask, notify)
    placed, skipped = 0, []
    for p in positions:
        if p.get("magic") not in (MAGIC_GRID_BUY, MAGIC_GRID_SELL):
            continue          # anchors are the owner's, never the engine's to stop
        is_buy = p.get("type") == "POSITION_TYPE_BUY"
        entry = float(p.get("openPrice", 0) or 0)
        if not entry:
            continue
        if has_protective_stop(p, max(0.0, float(ask) - float(bid))) or p.get("stopLoss"):
            continue
        want = entry_stop(entry, is_buy)
        if want is None:
            continue
        # Already past it? Then this is a close, not a stop. Leave it to the user.
        now = float(bid) if is_buy else float(ask)
        if (is_buy and now <= want) or (not is_buy and now >= want):
            skipped.append((p.get("id"), round((now - entry) * (1 if is_buy else -1), 2)))
            continue
        try:
            await asyncio.wait_for(conn.modify_position(p["id"], stop_loss=want), timeout=8.0)
            placed += 1
        except Exception as e:
            log.warning(f"entry stop refused on {p.get('id')}: {e}")
    if notify and (placed or skipped):
        lines = []
        if placed:
            lines.append(f"🔪 *حطيت وقف `{GRID_STOP_DIST:g}$` على `{placed}` صفقة مفتوحة*")
        if skipped:
            lines.append(f"• `{len(skipped)}` صفقة تجاوزت الوقف من زمان — ما حطيته عليها، "
                         f"لأن معناه إغلاق فوري بأسوأ سعر مو حماية")
        await notify("\n".join(lines))
    return placed


async def ensure_take_profits(conn, positions, notify=None):
    """Put the per-trade target on any grid position that is missing one.

    Attaching it only when the order is placed leaves every position opened before the
    setting existed - or by an order placed under an older build - running with no exit of
    its own. The user watched a level sit at +$2.43 and give it all back for exactly that
    reason, so the target is reconciled on what is open, not only on what is sent.
    """
    if GRID_TP_PER_TRADE <= 0:
        return 0
    fixed, moved = [], 0
    for p in positions:
        dist = tp_distance(p.get("volume", 0))
        if not dist:
            continue
        entry = float(p.get("openPrice", 0) or 0)
        if not entry:
            continue
        is_buy = p.get("type") == "POSITION_TYPE_BUY"
        tp = round(entry + dist, 2) if is_buy else round(entry - dist, 2)
        # Skipping anything that already HAS a target is the same mistake the trailing
        # stop made: change the figure from $5 to $1 and every live position keeps the
        # old, far one forever. What matters is whether the target is the CURRENT one.
        have = p.get("takeProfit")
        try:
            if have is not None and abs(float(have) - tp) < 0.01:
                continue
        except (TypeError, ValueError):
            pass
        if have:
            moved += 1
        try:
            await asyncio.wait_for(conn.modify_position(p["id"], take_profit=tp), timeout=8.0)
            fixed.append((p["id"], tp))
        except Exception as e:
            log.warning(f"could not set take profit on {p.get('id')}: {e}")
    if fixed and notify:
        what = (f"نقلت هدف `{moved}` صفقة للرقم الجديد و" if moved else "")
        await notify(f"🎯 *{what}حطيت هدف `+{GRID_TP_PER_TRADE:g}$` على "
                     f"`{len(fixed) - moved}` صفقة كانت بلا هدف* — "
                     f"تغلق لحالها عند الوسيط.")
    return len(fixed)


async def grid_orders(conn, symbol=None):
    if not hasattr(conn, "get_orders"):
        return []
    try:
        orders = await asyncio.wait_for(conn.get_orders(), timeout=6.0)
    except Exception as e:
        log.warning(f"could not read pending orders: {e}")
        return []
    return [o for o in orders
            if o.get("magic") in MAGICS and o.get("symbol") == (symbol or GRID_SYMBOL)]


async def cancel_all(conn):   # noqa: D401 - the record is cleared with the orders
    cancelled = 0
    for o in await grid_orders(conn):
        try:
            await asyncio.wait_for(conn.cancel_order(o["id"]), timeout=6.0)
            cancelled += 1
        except Exception as e:
            log.warning(f"could not cancel {o.get('id')}: {e}")
    forget_orders()
    return cancelled


async def close_all(conn):
    """Close every grid position. Returns (count, realized)."""
    closed, booked = 0, 0.0
    for p in await grid_positions(conn):
        try:
            booked += float(p.get("unrealizedProfit", 0.0) or 0.0)
            await asyncio.wait_for(conn.close_position(p["id"]), timeout=6.0)
            closed += 1
        except Exception as e:
            log.warning(f"could not close {p.get('id')}: {e}")
    return closed, round(booked, 2)


async def teardown(conn):
    closed, booked = await close_all(conn)
    cancelled = await cancel_all(conn)
    return closed, booked, cancelled


def cash_in_price(cash, lot=None):
    """What a cash figure is worth as a distance in the instrument's own price."""
    per = float(lot if lot is not None else GRID_BASE_LOT) * GRID_CONTRACT
    return round(float(cash) / per, 2) if per > 0 and cash else 0.0


def money(x):
    """A price distance a human can read on ANY instrument.

    "{:,.0f}" was chosen while the bot traded bitcoin, where a distance is hundreds of
    dollars. The same format turned gold's half-dollar into "0$" - a true number
    rendered as a false one, on the very line meant to explain the conversion. The
    precision follows the size instead.
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "?"
    a = abs(v)
    if a >= 100:
        return f"{v:,.0f}"
    if a >= 1:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{v:,.3f}".rstrip("0").rstrip(".") if a else "0"


def cycle_cost(spread, lot=None):
    """What one round trip costs before any rule earns a cent.

    The number an outside review called the most important one missing from this
    project, and it was right: a target smaller than a few multiples of this is a
    losing trade dressed as a winning rule, however well the rest behaves.
    """
    per = float(lot if lot is not None else GRID_BASE_LOT) * GRID_CONTRACT
    return round(abs(float(spread or 0.0)) * per, 2)


def cost_verdict(spread, exit_cash, lot=None):
    """(cost, share of the exit it eats, a word). None when there is nothing to judge."""
    cost = cycle_cost(spread, lot)
    if cost <= 0 or not exit_cash or exit_cash <= 0:
        return None
    share = round(cost / float(exit_cash) * 100, 0)
    return cost, share, ("\u26d4\ufe0f \u062e\u0637\u0631" if share > 33 else
                         "\u26a0\ufe0f \u062d\u062f\u0651\u064a" if share > 20 else "\u2705")


def protection_audit(price=0.0, lot=None):
    """Every cash-denominated protection setting, in price terms, with a verdict.

    These settings are priced in dollars of P/L, which is lot-independent and was the
    whole point. The catch is that their meaning as a DISTANCE changes the moment the
    lot does: a $3 trail is $150 of gold at 0.02 lots and $300 of bitcoin at 0.01.
    The owner halved his lot and the trail silently doubled its reach, so it never got
    the chance to move before price came back. Nothing on any screen said so.
    """
    lot = float(lot if lot is not None else GRID_BASE_LOT)
    rows, warn = [], []
    for name, key, cash in (("التأمين", "be", BE_TRIGGER_CASH),
                            ("المقفول", "belock", BE_LOCK_CASH),
                            ("الأرضية", "befloor", BE_FLOOR_CASH),
                            ("التريلينغ", "betrail", BE_TRAIL_CASH)):
        if cash <= 0:
            continue
        px = cash_in_price(cash, lot)
        # Judged against the grid's OWN geometry, not against an invented percentage.
        # A first attempt used fixed shares of price and promptly called the gold
        # settings - which demonstrably work on gold - too tight. The honest measure
        # is the ladder itself: a protection distance wider than the whole ladder can
        # never act inside the range the grid trades, whatever the instrument.
        ladder_depth = GRID_STEP * max(1, GRID_LEVELS)
        wide = ladder_depth > 0 and px > ladder_depth
        rows.append((name, key, cash, px, "⚠️ واسع" if wide else "✅"))
        if wide:
            good = round(ladder_depth * 0.5 * lot * GRID_CONTRACT, 2)
            warn.append(f"• `{name}` = حركة `{money(px)}$`، وكل سلّم الشبكة "
                        f"`{money(ladder_depth)}$` — ما يلحق يتحرك داخل مدى الشبكة. "
                        f"جرّب `/setkey {key} {good:g}`")
    return rows, warn


def plan_text(balance=0.0, price=0.0, spread=0.0):
    """Everything the user needs to judge the configuration before arming it."""
    lots = ladder(base=recovery_base(load().get("loss_streak", 0)))
    total = round(sum(lots), 2)
    margin_each = GRID_CONTRACT  # unknown price here; the caller fills real margin in
    # Quote the risk on what can actually be open at once, not on the full ladder: the
    # position cap is enforced for real now, so the old twelve-level figures were fiction
    # that made the grid look about twice as dangerous as it is.
    risk = exposure_lots(lots)
    worst, best, whip = worst_case(risk), best_case(risk), whipsaw_case(risk)
    limit = loss_limit(balance)
    shown = ", ".join(f"{x:g}" for x in lots[:10]) + (" …" if len(lots) > 10 else "")
    unit = UNIT_WORD.get(float(GRID_CONTRACT), f"{GRID_CONTRACT:g} وحدة")
    head_line = f"• 🎯 الأداة: `{GRID_SYMBOL}` · اللوت = `{unit}`"
    if price:
        pct = GRID_STEP / float(price) * 100.0
        ok_step, floor, nice = step_sane(price)
        head_line += f"\n• السعر `{float(price):,.0f}$` · الخطوة `{pct:.3f}%` منه"
        if not ok_step:
            head_line += (f"\n• ⛔️ *الخطوة صغيرة على هذا السعر — ما أنصب*\n"
                          f"  الحد `{floor:g}$` · المنصوح `{nice:g}$` · "
                          f"`/setkey gridstep {nice:g}`")
    lines = [
        head_line,
        f"• المستويات: `{len(lots)}` لكل جهة · المسافة `{GRID_STEP:g}$`",
        f"• السلّم: `{shown}`",
        f"• أكبر لوت: `{max(lots) if lots else 0:g}` · المجموع `{total:g}` لوت",
        f"• مدى الشبكة: `{round(len(lots) * GRID_STEP, 2):g}$` بكل اتجاه",
        (f"• 🎯 هدف كل صفقة لحالها: `+{GRID_TP_PER_TRADE:g}$` — محطوط عند الوسيط، "
         f"يغلق فوراً حتى لو البوت مطفي" if GRID_TP_PER_TRADE > 0
         else "• ماكو هدف لكل صفقة — الباسكت بس"),
        f"• جني الباسكت: ما يغلق قبل `+{GRID_TP_CASH:g}$`، وبعدها "
        + (f"يمشي معه ويغلق إذا رجع `{GRID_TRAIL_PCT:g}%` من القمة "
           f"(وما أقل من `{GRID_TRAIL_MIN:g}$` حتى ما يغلق على ضجيج)" if GRID_TRAIL_PCT > 0
           else "يغلق فوراً عند الهدف"),
        f"• وقف الشبكة: `-{limit:g}$`",
        (f"• 🔪 *وقف ثابت `{GRID_STOP_DIST:g}$` على كل أمر جديد* — تنحط عند الوسيط لحظة "
         f"الفتح، وما تُطبّق أبداً على صفقة مفتوحة من قبل"
         if GRID_STOP_DIST > 0 else "• ماكو وقف ثابت — الصفقة تمشي لحد ما تقرر انت"),
        (f"• ✂️ التنصيف: كل `{half_distance(price):g}$` يبتعد السعر عن صفقة خاسرة، "
         f"يغلق نصها (لحد `{HALF_MIN_LOT:g}`)"
         if HALF_ENABLED else "• ⚠️ التنصيف مطفي — الصفقة تبقى بحجمها مهما ابتعدت"),
        (f"• 🎯 *اتجاه مثبّت يدوياً: {bias_text()}* — ما ينصب ولا يسدّ فجوة بالجهة الثانية"
         if GRID_BIAS != "both" else "• ↔️ الجهتين شغالات"),
        (f"• 🧭 حارس الترند: إذا ابتعد السعر `{GRID_TREND_STEPS:g}` خطوات عن المركز وثبت "
         f"`{GRID_TREND_HOLD:g}` ثانية، يوقف الجهة اللي يدهسها ويمشي مع الحركة "
         f"(يرجّعها عند `{GRID_TREND_RELEASE:g}` خطوات)"
         if GRID_TREND else "• ⚠️ حارس الترند مطفي — الجهتين تنعبّى حتى بالترند"),
        (f"• 🔁 استرداد محدود: كل `{GRID_RECOVER_AFTER}` دورات خاسرة يزيد اللوت "
         f"`+{GRID_RECOVER_STEP:g}` ويقف عند "
         f"`{round(min(GRID_RECOVER_MAX, GRID_MAX_LOT), 2):g}` — جمع، مو مضاعفة"
         if GRID_RECOVER else "• الاسترداد المحدود مطفي — اللوت ثابت مهما خسرت الدورات"),
        ("• ✂️ يحصد كل جهة لحالها" if GRID_SIDE_HARVEST else "• يحصد المجموع الكلي"),
        (f"• 🛡 التأمين: أول ما تعبر `{BE_TRIGGER_CASH:g}$` ينقل وقفها لنقطة الفتح — "
         f"ما تصير خسارة بعدها أبداً\n"
         f"• 📈 بعدها يتبع الربح على بُعد `{BE_TRAIL_CASH:g}$` من القمة، "
         f"وأول ما تعدّي `{BE_FLOOR_CASH:g}$` ما ينزل الوقف تحتها"
         if BE_ENABLED else "• ⚠️ ماكو تأمين على نقطة الدخول"),
        *([f"• 💸 كلفة الدورة `{_cv[0]:g}$` (سبريد `{spread:g}$`) — "
           f"تاكل `{_cv[1]:.0f}%` من خروج نموذجي عند قمة `2$`  {_cv[2]}"]
          if (_cv := cost_verdict(spread, giveback_floor(2.0) or GRID_TP_PER_TRADE
                                  or GRID_TP_CASH)) else []),
        *( [f"• ⚖️ كل `1$` ربح = حركة `{money(cash_in_price(1.0))}$` بهذا اللوت"]
           + [f"• {n}: `{c:g}$` = حركة `{money(px)}$`  {v}"
              for n, _k, c, px, v in protection_audit(price)[0]]
           if price else []),
        *(protection_audit(price)[1] if price else []),
        (f"• 🎯 ارتداد `{TRADE_GIVEBACK_PCT:g}%` من قمة ربح الصفقة يغلقها "
         f"(وما ينزل تحت `{BE_LOCK_CASH:g}$` المضمونة)"
         if TRADE_GIVEBACK_PCT > 0 else
         "• ماكو إغلاق على ارتداد — التريلينغ بس"),
        (f"• 🧹 التصفية الممولة: صفقة ابتعد عنها السعر `{RESCUE_STEPS:g}` خطوات "
         f"تنغلق من المحصّل لما يكفي (المحجوز `{RESCUE_RESERVE:g}$`)"
         if RESCUE_ENABLED else "• ماكو تصفية ممولة للصفقات العالقة"),
        f"• 🚧 سقف الصفقات المفتوحة: `{GRID_MAX_POSITIONS}` · يوقف التوسّع عند "
        f"`{GRID_ADD_STOP_PCT:g}%` من حد الخسارة",
        (f"• 🧩 يسدّ الفجوات: أمر جديد إذا ابتعد أقرب مستوى أكثر من `{GRID_GAP_FACTOR:g}` خطوة"
         if GRID_REFILL else "• ⚠️ ما يسدّ الفجوات — ممكن يبقى بلا أوامر قرب السعر"),
        ("• 🤝 الخاسرة تبقى مفتوحة وتصير إلك — البوت ما يتحكم بيها"
         if GRID_RELEASE_LOSERS else "• الخاسرة تنغلق مع الرابحة"),
        f"• لو امتلأت جهة ورجع السعر للوسط: `-{worst:,.0f}$`",
        f"• لو تأرجح وكسر الجهتين: `-{whip:,.0f}$` ← وهنا يقطع الوقف عند `-{limit:g}$`",
        f"• لو امتلأت واستمر بنفس الاتجاه: `+{best:,.0f}$`",
        f"  _(محسوبة على `{len(risk)}` صفقة — سقف المفتوح، مو كل `{len(lots)}` مستوى)_",
    ]
    if balance:
        lines.append(f"• الوقف = `{round(limit / balance * 100)}%` من رصيدك `{balance:g}$` — "
                     "وهو السقف الحقيقي للخسارة، مو رقم التأرجح")
    _ = margin_each
    return "\n".join(lines)


async def arm(conn, symbol, notify, only_magic=None, max_lots=None, why="",
              max_orders=None):
    """Place the ladder. `only_magic` and `max_lots` restrict it to a hedging pass."""
    price = await asyncio.wait_for(conn.get_symbol_price(symbol=symbol), timeout=6.0)
    bid, ask = float(price.get("bid", 0) or 0), float(price.get("ask", 0) or 0)
    if not bid or not ask:
        return False, "ماكو سعر"
    center = round((bid + ask) / 2.0, 2)
    ok_step, floor, nice = step_sane(center)
    if not ok_step:
        return False, (f"الخطوة `{GRID_STEP:g}$` صغيرة جداً على سعر `{center:,.0f}$` — "
                       f"السلّم كله يمتلي بدقيقة وما يبقى شبكة.\n"
                       f"• الحد الأدنى `{floor:g}$`  • المنصوح `{nice:g}$`\n"
                       f"• `/setkey gridstep {nice:g}`")
    streak = load().get("loss_streak", 0)
    base = recovery_base(streak)
    lots = ladder(base=base)
    if not lots:
        return False, "ماكو مستويات"
    # A fresh ladder is a fresh direction question: whatever was stood down was stood
    # down against the OLD centre, and keeping it would leave the new grid half-armed.
    trend_reset()
    bias_reset()
    halves_reset()

    acct = await asyncio.wait_for(conn.get_account_information(), timeout=6.0)
    balance = float(acct.get("balance", 0) or 0)
    free = float(acct.get("freeMargin", acct.get("equity", balance)) or balance)
    leverage = float(acct.get("leverage", 500) or 500)
    _sides = 1 if only_magic is not None else bias_sides()
    _total = min(sum(lots), max_lots) if max_lots is not None else sum(lots)
    need = _total * _sides * center * GRID_CONTRACT / max(1.0, leverage)
    ceiling = balance * GRID_MARGIN_CEILING_PCT / 100.0
    if need > min(free, ceiling):
        return False, (f"الهامش المطلوب `{need:,.0f}$` أكبر من المسموح "
                       f"`{min(free, ceiling):,.0f}$` — قلّل المستويات أو اللوت")

    placed, failed, placed_lots = 0, 0, 0.0
    reasons = []                      # what the broker actually said, for the user
    skipped_dup = 0
    # What is ALREADY in the market. Arming twice - a re-centre, a restart, two
    # /grid on in a row - used to stack a second ladder on top of the first at the
    # same prices, doubling the exposure with no message anywhere. One read.
    #
    # And if that read FAILS we must not carry on with an empty list, because an
    # empty list means "nothing is out there" and that is precisely the sentence
    # this project has been punished for four times. Being unable to see the book
    # is a reason to place nothing, not a reason to place everything. Protection
    # is untouched by this: it only ever tightens a stop.
    #
    # A broker that CANNOT be asked is not the same as an ask that FAILED. The paper
    # broker and some adapters expose no order book at all; refusing to arm on those
    # would be a lockout dressed as caution. Only a reader that exists and then threw
    # means we are blind.
    # A ladder next to a live single-trade test is exactly the extra exposure that
    # mode promises not to create. The general lock refuses these orders anyway
    # today, so this guard changes nothing right now - which is the point: it is
    # the guard that still holds on the day the general lock is lifted, and a
    # guard written and never wired is a comment pretending to be a control.
    if safety.test_mode_active():
        return False, ("\u0645\u0627 \u0646\u0635\u0628\u062a: \u0648\u0636\u0639 \u0627\u062e\u062a\u0628\u0627\u0631 \u0627\u0644\u062a\u0646\u0641\u064a\u0630 \u0641\u0639\u0651\u0627\u0644 \u2014 \u0635\u0641\u0642\u0629 \u0648\u062d\u062f\u0629 \u0628\u0633\u060c "
                       "\u0648\u0627\u0644\u0634\u0628\u0643\u0629 \u0645\u0642\u0641\u0648\u0644\u0629 \u0637\u0648\u0644 \u0627\u0644\u0648\u0642\u062a. `/testtrade off` \u064a\u0633\u062d\u0628 \u0627\u0644\u0625\u0630\u0646.")

    _open, _book_ok = [], True
    for _getter in ("get_orders", "get_positions"):
        _fn = getattr(conn, _getter, None)
        if _fn is None:
            continue
        try:
            _open += list(await asyncio.wait_for(_fn(), timeout=6.0) or [])
        except Exception as e:
            log.warning(f"could not read {_getter} before arming: {e}")
            _book_ok = False
            break
    _open = [o for o in _open if o.get("symbol") == symbol]
    _ok, _why = safety.entry_allowed(time.time(), ENTRY_MAX_BLIND_SECS,
                                     connected=_book_ok)
    if not _ok:
        return False, ("ما نصبت: " + _why + " — ما أكدر أشوف شنو "
                       "منصوب أصلاً، والنصب على عمى يضاعف المخاطرة. "
                       "الحراسة على الصفقات المفتوحة مستمرة.")
    # Half a step: two rungs closer together than that are the same rung twice.
    _dup_tol = max(GRID_STEP * 0.5, 0.01)
    # GRID_MAX_POSITIONS is a promise about OPEN trades, but what enforces it is how
    # many triggers are sitting in the market. Arming six levels a side on both sides
    # leaves twelve, against a cap of eight - and a whipsaw fills them faster than the
    # engine's three-second tick can pull the rest. Simulated: nine open against a cap
    # of eight. Placing no more than the cap allows makes the number mean what it says.
    for i, lot in enumerate(lots, start=1):
        up = round(center + i * GRID_STEP, 2)
        down = round(center - i * GRID_STEP, 2)
        dist = tp_distance(lot)
        for maker, price_at, magic, sign in (
                (conn.create_stop_buy_order, up, MAGIC_GRID_BUY, 1),
                (conn.create_stop_sell_order, down, MAGIC_GRID_SELL, -1)):
            if not bias_allows(magic):
                continue              # the user has stood this direction down by hand
            if only_magic is not None and magic != only_magic:
                continue              # hedging pass: one side only
            if max_lots is not None and placed_lots + lot > max_lots + 1e-9:
                continue              # never hedge past flat and open fresh exposure
            if max_orders is not None and placed >= max_orders:
                continue              # see below: never leave more triggers than the cap
            if safety.would_duplicate(price_at, sign > 0, _open, _dup_tol):
                skipped_dup += 1
                continue              # this rung is already in the market
            tp = round(price_at + sign * dist, 2) if dist else None
            try:
                opts = {"magic": magic}
                _tsl = server_trail()
                if _tsl:
                    opts["trailingStopLoss"] = _tsl   # protected from the first tick
                require_entry_permission(stop_loss=entry_stop(price_at, sign > 0), lot=lot,
                                         conn=conn)
                res = await asyncio.wait_for(
                    maker(symbol=symbol, volume=lot, open_price=price_at,
                          stop_loss=entry_stop(price_at, sign > 0),
                          take_profit=tp, options=opts), timeout=8.0)
                remember_order((res or {}).get("orderId"))
                placed += 1
                placed_lots = round(placed_lots + lot, 2)
            except safety.EntryBlocked as e:
                # Our own lock, not the broker. Printing this under "the broker
                # said" sent the owner looking for a problem at his broker.
                failed += 1
                why = "🔒 " + (str(e).strip()[:160] or "entry locked")
                if why not in reasons:
                    reasons.append(why)
                continue
            except Exception as e:
                failed += 1
                why = str(e).strip()[:160] or e.__class__.__name__
                if why not in reasons:
                    reasons.append(why)
                log.warning(f"grid level {i} at {price_at} failed: {e}")
    if not placed and skipped_dup:
        return False, (f"كل المستويات منصوبة من قبل (`{skipped_dup}` رتبة) — "
                       f"ما ضاعفت شي. `/grid reset` يلغي وينصب من جديد.")
    if not placed:
        # "Check the symbol and the lot size" was a guess printed INSTEAD of the answer:
        # the broker had said exactly what was wrong, every time, and it went to the log
        # file the owner cannot read from his phone. Hand him what the broker said.
        detail = "\n".join(f"• `{r}`" for r in reasons[:3]) or "• (الوسيط ما رد بسبب)"
        src = ("قفل الأمان رفض" if any(r.startswith("🔒") for r in reasons)
               else "رد الوسيط")
        return False, (f"ما انحطت ولا أمر. {src}:\n{detail}\n"
                       f"• الأداة `{symbol}` · اللوت `{lots[0]:g}` · العقد "
                       f"`{GRID_CONTRACT:g}` · الخطوة `{GRID_STEP:g}$`"
                       + (f"\n• الوقف الثابت `{GRID_STOP_DIST:g}$` — على أداة بسعر "
                          f"`{center:,.0f}$` هذا أقرب من حد الوسيط غالباً، "
                          f"جرّب `/setkey gridstop 0`" if GRID_STOP_DIST > 0 else ""))

    state = load()
    state.update({"armed": True, "center": center, "armed_at": time.time(),
                  "halted": False, "halt_reason": ""})
    save(state)
    note = f"\n⚠️ `{failed}` أمر ما انحط." if failed else ""
    if GRID_BIAS != "both":
        side_ar = "البيع" if GRID_BIAS == "down" else "الشراء"
        other_ar = "الشراء" if GRID_BIAS == "down" else "البيع"
        note += (f"\n🎯 *اتجاه واحد: {bias_text()}* — نصبت جهة {side_ar} بس، "
                 f"وما لمست جهة {other_ar}.")
    rec = ""
    if base > round(min(GRID_BASE_LOT, GRID_MAX_LOT), 2):
        rec = (f"\n🔁 *استرداد محدود فعّال* — `{streak}` دورة خاسرة ورا بعض، "
               f"فبديت من `{base:g}` بدل `{GRID_BASE_LOT:g}`. "
               f"السقف `{round(min(GRID_RECOVER_MAX, GRID_MAX_LOT), 2):g}` وما يتعداه أبداً.")
    head = why or f"🕸 *نصبت الشبكة حول* `{center}`"
    # The arming message is the one the owner actually reads, and it was the one
    # calling plan_text with no price and no spread. cost_verdict returns None on a
    # zero spread, so the cycle-cost line - the warning that a round trip eats a
    # third of a typical exit - was silently dropped from exactly that message. The
    # real bid and ask are right here; there was never a reason not to pass them.
    dup = (f"\n♻️ تجاوزت `{skipped_dup}` رتبة منصوبة من قبل — ما ضاعفت المخاطرة."
           if skipped_dup else "")
    if notify:
        await notify(f"{head}\n\n{plan_text(balance, center, round(ask - bid, 5))}\n"
                     f"• أوامر معلّقة: `{placed}`{note}{rec}{dup}")
    return True, None


_balance_cache = {"at": 0.0, "value": 0.0, "equity": 0.0}


async def cached_account(conn):
    """(balance, equity) for the whole account, one cached read for both."""
    now = time.time()
    if now - _balance_cache["at"] < GRID_BALANCE_TTL and _balance_cache["value"]:
        return _balance_cache["value"], _balance_cache["equity"]
    acct = await asyncio.wait_for(conn.get_account_information(), timeout=6.0)
    bal = float(acct.get("balance", 0) or 0)
    eq = float(acct.get("equity", bal) or bal)
    _balance_cache.update({"at": now, "value": bal, "equity": eq})
    note_equity(eq)                # the one place a fresh equity is actually observed
    return bal, eq


async def cached_balance(conn):
    bal, _ = await cached_account(conn)
    return bal


def net_exposure(positions, symbol=None):
    """Signed lots across every position given: + is net long, - is net short.

    Counts everything on the symbol - grid, anchor, trades handed over - because
    direction, like margin, does not care which engine opened what.
    """
    net = 0.0
    for p in positions or []:
        if symbol and p.get("symbol") != symbol:
            continue
        try:
            vol = float(p.get("volume", 0) or 0)
        except (TypeError, ValueError):
            continue
        net += vol if p.get("type") == "POSITION_TYPE_BUY" else -vol
    return round(net, 2)


def hedge_side(net):
    """The side that moves an account TOWARD flat, or None when it already is.

    This is the correction the owner pointed out. The drawdown brake blocked every new
    order while the account was under water, on the reasoning that a new order is new
    exposure. For one side of the book that is simply false: he is long and losing, so
    a SELL does not add risk - it cancels some. Blocking it kept him pinned to the only
    direction that was hurting him, which is the opposite of what a brake is for.
    """
    if net > 0:
        return MAGIC_GRID_SELL      # long and hurting: selling moves toward flat
    if net < 0:
        return MAGIC_GRID_BUY
    return None


def equity_peak():
    """The highest equity this account has reached, remembered across restarts."""
    return float(load().get("equity_hwm", 0.0) or 0.0)


def note_equity(equity):
    """Record a newly OBSERVED equity. Called where the account is actually read.

    Kept separate from drawdown_brake on purpose. Folding it in made a function that
    reads like arithmetic write to disk, so merely asking "are we braked?" moved the
    high-water mark - and two tests that call it with made-up numbers silently taught
    the file a peak that never happened. A query that mutates is a trap for whoever
    reads it next.
    """
    try:
        eq = float(equity)
    except (TypeError, ValueError):
        return equity_peak()
    st = load()
    hwm = float(st.get("equity_hwm", 0.0) or 0.0)
    if eq > hwm:
        hwm = round(eq, 2)
        st["equity_hwm"] = hwm
        save(st)
    return hwm


def drawdown_brake(balance, equity):
    """How far under its own high-water mark the account is, and whether that forbids
    expansion.

    This used to measure balance minus equity, which only sees FLOATING loss. Closing a
    loser turns that loss into balance and the measure reads zero - so the moment the
    account is hurt most, the brake reports it healthy. The owner's own account proved
    it: he closed roughly $1,886 of losing gold, the balance fell to $1,885, and the
    drawdown read 0 on an account that had lost half of itself. Same family as "cannot
    see" read as "nothing there", and five passes of review did not catch it.

    Measured against the highest equity the account has reached, a realised loss stays
    counted - which is what a drawdown means everywhere else in this trade.
    """
    if GRID_EQUITY_BRAKE_PCT <= 0 or balance <= 0:
        return 0.0, 0.0, False
    eq = float(equity)
    hwm = max(equity_peak(), eq)   # read only; note_equity() is what records it
    # Floating-only, the old measure: still the right answer early on, before the
    # account has ever been in profit, when the peak IS roughly the starting balance.
    floating = max(0.0, float(balance) - eq)
    down = round(max(floating, hwm - eq), 2)
    limit = round(max(hwm, float(balance)) * GRID_EQUITY_BRAKE_PCT / 100.0, 2)
    return down, limit, down >= limit


# How often the engine repeats "I cannot lay a ladder yet". Often enough to be seen,
# rarely enough not to become noise on a grid that sits at its cap for an hour.
BLOCKED_NOTE_SECS = _f("BLOCKED_NOTE_SECS", 900.0)
_blocked_at = 0.0


async def grid_engine(get_conn, symbol, notify, is_active):
    """Watch the basket: take it on profit, cut it on loss, re-centre when it empties."""
    warned_unsupported = False
    arm_fails = 0
    set_symbol(symbol)            # the argument wins over whatever the env said
    # best each side has shown; in memory, so a peak dies with the basket that earned it
    peaks = {"buy": 0.0, "sell": 0.0, "all": 0.0}
    while True:
        try:
            if not (GRID_ENABLED and is_active()):
                # The quietest failure in the whole project. /grid on sets GRID_ENABLED,
                # answers "the grid is on", restarts - and then this line sends the
                # engine straight back to sleep because BOT_ACTIVE is still 0 from an
                # earlier /pause. Nothing connected the two, so the owner watched an
                # empty chart while the bot insisted the grid was running. Say it.
                if GRID_ENABLED and not is_active():
                    global _paused_said
                    if time.time() - _paused_said > BLOCKED_NOTE_SECS:
                        _paused_said = time.time()
                        await notify("⏸ *الشبكة مشغّلة بس النظام موقوف* — ما أنصب شي.\n"
                                     "• أرسل `/resume` حتى أبدأ\n"
                                     "• (النظام انوقف بـ `/pause` أو `/stop` من قبل)")
                await asyncio.sleep(3)
                continue
            conn = get_conn()
            if conn is None:
                await asyncio.sleep(3)
                continue
            global _last_conn
            _last_conn = conn
            # Blind for too long? Pull the orders - but only if a fill would actually
            # be unmanaged. Once every order is born carrying a server-side trailing
            # stop, MetaApi protects whatever it opens whether or not we can see it, so
            # cancelling would just throw away the user's position for no safety gained.
            # That is what happened at 2:43: six sell orders he wanted, pulled because
            # the premise "a fill is unwatched" was no longer true.
            if (not reads_free(conn) and server_trail() is None
                    and blind_for() > BLIND_SECS and not _blind["cut"]):
                _blind["cut"] = True
                try:
                    await cancel_known(conn, notify)
                except Exception as e:
                    log.warning(f"blind cancel failed: {e}")
            wait = cooling_down()
            if wait and not reads_free(conn):
                await asyncio.sleep(min(wait, 30.0))
                continue
            if not hasattr(conn, "create_stop_buy_order"):
                if not warned_unsupported:
                    warned_unsupported = True
                    await notify("⚠️ الشبكة تحتاج أوامر معلّقة، وهذا الوسيط ما يدعمها.")
                await asyncio.sleep(60)
                continue
            warned_unsupported = False

            state = load()
            if state.get("halted"):
                # Silent forever. A halt set hours ago on another instrument stops the
                # engine dead and nothing on any screen says why, which is exactly the
                # shape of bug this project keeps paying for.
                global _halt_said
                if time.time() - _halt_said > BLOCKED_NOTE_SECS:
                    _halt_said = time.time()
                    await notify(f"🛑 *الشبكة موقوفة* — "
                                 f"`{state.get('halt_reason', 'بدون سبب مسجل')}`\n"
                                 f"• `/grid on` يلغي الإيقاف ويبدأ من جديد")
                await asyncio.sleep(5)
                continue
            # Being unable to ARM must never stop us MANAGING what is already open.
            # Both of the blocks below used to `continue`, so a braked or crowded grid
            # that held positions skipped the harvest, the basket stop and everything
            # else for as long as the condition lasted - the trades were simply dropped.
            if not state.get("armed"):
                braked_now = False
                try:
                    _b, _e = await cached_account(conn)
                    _d, _lim, braked_now = drawdown_brake(_b, _e)
                except Exception as e:
                    log.warning(f"could not read the account for the brake: {e}")
                held, net, elsewhere = await book(conn, symbol)
                crowded_now = len(held) >= GRID_MAX_POSITIONS
                # Under water, but one direction still REDUCES the hole. Let that one
                # through, capped so it can never run past flat into fresh exposure.
                hedge = hedge_side(net) if braked_now else None
                if hedge is not None and bias_allows(hedge) and not crowded_now:
                    room_lots = round(abs(net), 2)
                    side_ar = "بيع" if hedge == MAGIC_GRID_SELL else "شراء"
                    ok, err = await arm(
                        conn, symbol, notify, only_magic=hedge, max_lots=room_lots,
                        why=(f"🛡 *نصبت سلّم {side_ar} للتحوّط*\n"
                             f"• الحساب نازل `{_d:g}$` وصافي تعرّضك "
                             f"`{abs(net):g}` لوت {'شراء' if net > 0 else 'بيع'}\n"
                             f"• كل صفقة {side_ar} تنقّص تعرّضك، ما تزيده\n"
                             f"• ما أتعدى `{room_lots:g}` لوت — عند هذا الحد تصير متعادل، "
                             f"وبعدها أي زيادة تكون مخاطرة جديدة فأوقف"))
                    if not ok:
                        log.warning(f"hedge arm refused: {err}")
                    else:
                        await asyncio.sleep(max(2, 0 if reads_free()
                                                else TICK_SECS_METERED))
                        continue
                if braked_now:
                    # A new ladder is new exposure. Not while the account is this far
                    # under - that is the moment it can least afford another level.
                    global _brake_said
                    if time.time() - _brake_said > BLOCKED_NOTE_SECS:
                        _brake_said = time.time()
                        # The drawdown that stops a bitcoin ladder can be entirely
                        # gold's. Without saying so, the owner sees a bot refusing to
                        # work on an instrument he has not lost a cent on.
                        other = ""
                        if elsewhere < -0.01:
                            need = int(abs(elsewhere) / max(1.0, _b) * 100) + 5
                            other = (f"• ⚠️ `{abs(elsewhere):g}$` من الهبوط من *أدوات "
                                     f"ثانية* مو من `{symbol}` — الشبكة هنا نظيفة\n"
                                     f"• ارفع المكبح فوق هذا: `/setkey brake {need}`\n")
                        await notify(
                            f"🛑 *ما أنصب شبكة جديدة — الحساب نازل `{_d:g}$` تحت رصيده*\n"
                            f"• الحد `{_lim:g}$` (`{GRID_EQUITY_BRAKE_PCT:g}%` من الرصيد)\n"
                            f"{other}"
                            f"• ما أضيف ولا صفقة تزيد تعرّضك وأنت بهذا العمق\n"
                            f"• بس أبقى أدير `{len(held)}` صفقة مفتوحة: تأمين، تريلينغ، "
                            f"وحصاد لو طلعت بالربح\n"
                            f"• أرجع أنصب أول ما ترجع الخسارة تحت الحد\n"
                            f"• `/setkey brake 0` يلغي المكبح (ما أنصح)")
                elif crowded_now:
                    global _blocked_at
                    if time.time() - _blocked_at > BLOCKED_NOTE_SECS:
                        _blocked_at = time.time()
                        lots = ladder(base=recovery_base(state.get("loss_streak", 0)))
                        await notify(
                            f"⏳ *ما أقدر أنصب سلّم جديد* — عندي `{len(held)}` صفقة مفتوحة "
                            f"وهذا سقف الصفقات (`{GRID_MAX_POSITIONS}`).\n"
                            f"• أي إعداد غيّرته للوت محفوظ وينطبق على السلّم الجاي: "
                            f"`{', '.join(f'{x:g}' for x in lots[:6])} …`\n"
                            "• وأبقى أدير الصفقات المفتوحة عادي — تأمين وتريلينغ وحصاد\n"
                            "• وأنصب السلّم تلقائياً أول ما تنفك صفقة وحدة\n"
                            "• تريده هسه؟ سكّر صفقة بالمنصة، أو `/grid off` ثم `/grid on`")
                else:
                    ok, err = await arm(conn, symbol, notify,
                                        max_orders=max(0, GRID_MAX_POSITIONS - len(held)))
                    if ok:
                        arm_fails = 0
                    else:
                        # A refused batch is not a reason to halt forever. It used to
                        # be: one failure at 18:15, caused by a $1 stop left over from
                        # gold, latched halted=True - and every fix applied afterwards
                        # was irrelevant because the halt outlived all of them, in
                        # silence. Hours went into chasing settings that were already
                        # correct. Retry instead, and only give up after several
                        # consecutive failures, saying plainly that it will keep trying.
                        arm_fails += 1
                        if arm_fails >= ARM_FAIL_HALT:
                            state["halted"] = True
                            state["halt_reason"] = err or "?"
                            save(state)
                            await notify(f"🛑 *وقفت الشبكة بعد `{arm_fails}` محاولات*\n"
                                         f"{err}\n• `/grid on` يلغي الإيقاف ويبدأ من جديد")
                        elif time.time() - _armfail_said > BLOCKED_NOTE_SECS:
                            globals()["_armfail_said"] = time.time()
                            await notify(f"⚠️ ما كدرت أنصب الشبكة "
                                         f"(محاولة `{arm_fails}` من `{ARM_FAIL_HALT}`):\n"
                                         f"{err}\n• أبقى أحاول — صلّح الإعداد وأنصب لحالي")
                    # Paced like the main path. An unarmed grid still calls book()
                    # every tick, so a flat 2-second retry is 10,800 position reads in
                    # six hours against a quota of about 4,320 - the same shape as the
                    # outage that blinded the bot before, just on the other engine.
                    await asyncio.sleep(max(2 if ok else ARM_RETRY_SECS,
                                            0 if reads_free() else TICK_SECS_METERED))
                    continue
                if not held:
                    # Idle, but still reading the book each pass. Free reads can keep
                    # the 5-second rhythm; metered ones must not.
                    await asyncio.sleep(5 if reads_free() else TICK_SECS_METERED)
                    continue
                # ...otherwise fall through and manage what is open.

            positions = await grid_positions(conn)
            if hasattr(conn, "modify_position"):
                await ensure_take_profits(conn, positions, notify)
            orders = await grid_orders(conn)
            buys = [p for p in positions if p.get("magic") == MAGIC_GRID_BUY]
            sells = [p for p in positions if p.get("magic") == MAGIC_GRID_SELL]
            floating = pnl(positions)

            _bal, _eq = await cached_account(conn)
            limit = loss_limit(_bal)
            down, brake_at, braked = drawdown_brake(_bal, _eq)

            # Each side is judged on its own when side-harvesting, so a winning direction is
            # not held hostage by the other one bleeding at the same rate.
            groups = ([("buy", buys, sells), ("sell", sells, buys)] if GRID_SIDE_HARVEST
                      else [("all", positions, [])])
            taken = None
            for name, group, other in groups:
                if not group:
                    peaks[name] = 0.0
                    continue
                here = pnl(group)
                if here > peaks[name]:
                    peaks[name] = here
                give_back = (round(max(peaks[name] * GRID_TRAIL_PCT / 100.0, GRID_TRAIL_MIN), 2)
                             if GRID_TRAIL_PCT > 0 else 0.0)
                # Rounded: 5.6 - 0.56 is 5.039999... in binary, so an exact 5.04 would miss.
                if here >= GRID_TP_CASH and here <= round(peaks[name] - give_back, 2):
                    taken = (name, group, other, peaks[name])
                    break

            # A winner nobody can take. With side-harvest off the exit is the BASKET's
            # net, so one trade at +$8 inside a basket at -$1900 can never be closed by
            # any rule - it just sits there. He watched exactly that happen. Say it.
            if (GRID_TP_PER_TRADE <= 0 and floating < 0 and not GRID_SIDE_HARVEST
                    and any(float(p.get("unrealizedProfit", 0) or 0) >= GRID_TP_CASH
                            for p in positions)):
                global _stranded_said
                if time.time() - _stranded_said > BLOCKED_NOTE_SECS:
                    _stranded_said = time.time()
                    await notify(
                        f"⚠️ *عندك صفقة رابحة ما أقدر أغلقها*\n"
                        f"• الحصاد مضبوط على *مجموع الشبكة* (`{floating:g}$`) مو على "
                        f"الصفقة، والمجموع ما راح يوصل `+{GRID_TP_CASH:g}$`\n"
                        f"• وماكو هدف لكل صفقة (`GRID_TP_PER_TRADE` = 0)\n"
                        f"• يعني الرابحة رهينة الخاسرات\n"
                        f"• الحل: `/setkey gridtpone {GRID_TP_CASH:g}` — كل صفقة تاخذ "
                        f"هدفها عند الوسيط وتنغلق لحالها")

            if taken:
                name, group, other, rode = taken
                closed, booked = await close_positions(conn, group)
                handed = release(other) if (GRID_RELEASE_LOSERS and other) else 0
                if not handed:
                    extra, more = await close_positions(conn, other)
                    closed, booked = closed + extra, round(booked + more, 2)
                cancelled = await cancel_all(conn)
                peaks = {"buy": 0.0, "sell": 0.0, "all": 0.0}
                state = load()
                state["cycles"] = state.get("cycles", 0) + 1
                state["realized"] = round(state.get("realized", 0.0) + booked, 2)
                # One rule, both ways: a cycle that books money clears the run, a cycle
                # that costs money lengthens it. That run is all recovery sizing reads.
                state["loss_streak"] = (0 if booked > 0
                                        else state.get("loss_streak", 0) + 1)
                state["armed"] = False
                save(state)
                side_ar = {"buy": "الشراء", "sell": "البيع", "all": "الشبكة"}[name]
                top = f" (القمة كانت `+{rode:g}$`)" if rode > booked else ""
                msg = [f"💰 *حصدت {side_ar}* `+{booked:g}$`{top}",
                       f"• أغلقت `{closed}` صفقة وألغيت `{cancelled}` أمر معلّق"]
                if handed:
                    msg.append(f"🤝 تركت لك `{handed}` صفقة خاسرة مفتوحة — صارت إلك، "
                               "ما أتحكم بيها ولا أحسبها علي.")
                msg.append(f"• الدورة رقم `{state['cycles']}` · الإجمالي `{state['realized']:g}$`")
                msg.append("🕸 أنصبها من جديد على السعر الحالي...")
                await notify("\n".join(msg))
                continue

            if positions and floating <= -limit:
                closed, booked, cancelled = await teardown(conn)
                peaks = {"buy": 0.0, "sell": 0.0, "all": 0.0}
                state.update({"armed": False, "halted": True,
                              "halt_reason": f"الخسارة وصلت {booked:g}$",
                              "realized": round(state.get("realized", 0.0) + booked, 2),
                              "loss_streak": state.get("loss_streak", 0) + 1})
                save(state)
                # Deliberately does not re-arm. A grid that restarts itself after a stop
                # is a grid that takes the account on the second try instead of the first.
                await notify(
                    f"🛑 *أوقفت الشبكة — وصلت حد الخسارة*\n\n"
                    f"• أغلقت `{closed}` صفقة بخسارة `{booked:g}$`\n"
                    f"• ألغيت `{cancelled}` أمر معلّق\n"
                    f"• الحد كان `-{limit:g}$`\n\n"
                    "ما أعيد نصبها لوحدي. راجع الإعدادات وأرسل `/grid on` لما تقرر.")
                continue

            # One price read per tick, shared by everything below it.
            try:
                quote = await quote_now(conn, symbol)
            except Exception as e:
                log.warning(f"tick has no price: {e}")
                quote = None

            # Shrink what price has run away from, before anything else looks at the
            # book. It returns True when it closed part of a position, and then the
            # lists above are stale, so the tick restarts rather than acting on them.
            if quote is not None:
                _b = float(quote.get("bid", 0) or 0)
                _a = float(quote.get("ask", 0) or 0)
                if _b and _a and await halve_distant(conn, positions, _b, _a, notify):
                    await asyncio.sleep(0.2)
                    continue

            # Stop feeding the side price is running over. Read off price against the
            # centre, so it names the side that is actually losing - see trend_read.
            stood_down = await trend_guard(conn, symbol, state.get("center"), orders,
                                           notify, quote=quote)
            await bias_watch(notify)
            if stood_down is not None:
                orders = [o for o in orders if o.get("magic") != stood_down]

            # How much more the grid may take on, in positions and in money. Both are
            # hard: the loss that triggered these ceilings came from twenty positions
            # opening together while every individual rule was behaving correctly.
            room = max(0, GRID_MAX_POSITIONS - len(positions))
            near_stop = floating <= -(limit * GRID_ADD_STOP_PCT / 100.0)
            if near_stop or braked:
                room = 0                  # near the stop, or the account is too deep down
            if room == 0:
                # The cap used to be checked only where the grid ADDED levels. But the
                # ladder arms with GRID_LEVELS pendings a side and every one of them
                # could still fire after the cap was reached, so on the one path that
                # mattered - a trend sweeping the ladder - the cap did nothing. Pull the
                # pendings: the grid keeps what it holds and can open nothing more until
                # something closes, and then refill walks levels back toward price.
                pulled = await cancel_all(conn) if orders else 0
                orders = []
                if not state.get("crowded"):
                    state["crowded"] = True
                    save(state)
                    why = (f"الحساب نازل `{down:g}$` تحت رصيده "
                           f"(الحد `{brake_at:g}$` = `{GRID_EQUITY_BRAKE_PCT:g}%`)" if braked
                           else "الخسارة قربت من الوقف" if near_stop
                           else f"وصلت سقف الصفقات `{GRID_MAX_POSITIONS}`")
                    await notify(f"🚧 *وقفت التوسّع* — {why}.\n"
                                 f"• عندي `{len(positions)}` صفقة مفتوحة\n"
                                 f"• سحبت `{pulled}` أمر معلّق حتى ما ينفتح شي جديد\n"
                                 "الصفقات المفتوحة تكمل بوقفها الخاص.")
            elif state.get("crowded"):
                state["crowded"] = False
                save(state)
                await notify("✅ *انفكّت الزحمة* — رجعت أنصب مستويات قرب السعر.")

            if orders or positions:
                await refill_gaps(conn, symbol, positions, orders, notify,
                                  skip_magic=stood_down, room=room, quote=quote)

            if not positions and not orders:
                peaks = {"buy": 0.0, "sell": 0.0, "all": 0.0}
                state["armed"] = False
                save(state)
                continue

            # Nothing open, but stale orders left behind the market: re-lay the ladder
            # around price instead of waiting for the last one to be hit or cancelled.
            # Only with no positions - cancelling pendings while trades are open could
            # pull a level that is hedging one of them.
            if (not positions and orders and GRID_RECENTER_STEPS > 0
                    and GRID_STEP > 0 and quote):
                _mid = (float(quote.get("bid", 0) or 0)
                        + float(quote.get("ask", 0) or 0)) / 2.0
                _centre = float(state.get("center", 0) or 0)
                _drift = abs(_mid - _centre) / GRID_STEP if (_mid and _centre) else 0.0
                _far = min((abs(float(o.get("openPrice", 0) or 0) - _mid)
                            for o in orders), default=0.0)
                if _drift >= GRID_RECENTER_STEPS and _far > GRID_STEP * GRID_GAP_FACTOR:
                    pulled = await cancel_all(conn)
                    state["armed"] = False
                    save(state)
                    await notify(
                        f"♻️ *أعدت توسيط الشبكة* — السعر ابتعد "
                        f"`{_drift:.1f}` خطوات عن المركز وماكو صفقة مفتوحة.\n"
                        f"• سحبت `{pulled}` أمر كان واقف ورا السوق\n"
                        f"• أنصب سلّم جديد حول `{_mid:,.0f}$` خلال ثواني")
                    continue

            # A trailing exit is only as tight as how often it looks. While a basket is in
            # the money the give-back threshold is live, so watch it three times as closely;
            # the rest of the time there is nothing to miss.
            # Sub-second ticking is only safe against a local cache. On the metered API
            # it is what burned 150,000 requests through a six-hour quota, after which
            # every read failed and the bot looked asleep.
            fast = 0.5 if floating >= GRID_TP_CASH else 1.5
            await asyncio.sleep(fast if reads_free() else TICK_SECS_METERED)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if note_api_error(e):
                log.warning("grid engine paused on a quota refusal")
                await asyncio.sleep(10)
                continue
            log.error(f"grid engine error: {e}", exc_info=True)
            await asyncio.sleep(5)


def status_text(balance=0.0, price=0.0, spread=0.0):
    state = load()
    lots = ladder(base=recovery_base(state.get("loss_streak", 0)))
    head = "🕸 *شبكة المضاعفة المكبوحة*"
    if not GRID_ENABLED:
        head += " — ❌ مطفية"
    elif state.get("halted"):
        head += f" — 🛑 موقوفة ({state.get('halt_reason', '؟')})"
    elif state.get("armed"):
        head += f" — ✅ منصوبة حول `{state.get('center')}`"
    else:
        head += " — ⏳ تنتظر النصب"
    body = [head, "", plan_text(balance, price, spread), "",
            f"• دورات مكتملة: `{state.get('cycles', 0)}` · محصّلة: `{state.get('realized', 0.0):g}$`"]
    freed = len(state.get("released", []))
    if freed:
        body.append(f"• 🤝 `{freed}` صفقة سلّمتها لك — مفتوحة بإدارتك، خارج حساب الشبكة")
    streak = state.get("loss_streak", 0)
    if streak:
        nxt = recovery_base(streak)
        tail = (f" · اللوت الجاي `{nxt:g}`" if GRID_RECOVER and nxt > GRID_BASE_LOT
                else " · اللوت ما يتغير (الاسترداد مطفي)" if not GRID_RECOVER else "")
        body.append(f"• 📉 دورات خاسرة ورا بعض: `{streak}`{tail}")
    if GRID_BIAS != "both":
        body.append(f"• 🎯 الاتجاه مثبّت على *{bias_text()}* — `/grid both` يرجّع الجهتين")
    if _trend["down"] is not None:
        side_ar = "البيع" if _trend["down"] == MAGIC_GRID_SELL else "الشراء"
        body.append(f"• 🧭 حارس الترند شغّال — جهة {side_ar} موقوفة مؤقتاً")
    if lots and max(lots) >= GRID_MAX_LOT:
        body.append(f"• 🔒 السقف `{GRID_MAX_LOT:g}` فعّال — بدونه أكبر لوت كان "
                    f"`{round(GRID_BASE_LOT * 2 ** ((len(lots) - 1) // GRID_DOUBLE_EVERY), 2):g}`")
    body.append("\n`/grid on` · `/grid off` · `/grid rearm` (إعادة نصب بدون إغلاق) · "
                "`/grid center` (مع إغلاق)")
    body.append("🎯 الاتجاه: `/grid down` (بيع فقط) · `/grid up` (شراء فقط) · "
                "`/grid both` (الجهتين)")
    return "\n".join(body)


# --- watching what was handed over ---------------------------------------------------
_released_seen = {}        # id -> {"worst": float, "green": bool, "last": float}
_released_summary_at = 0.0


def released_lines(positions):
    out = []
    for p in sorted(positions, key=lambda q: float(q.get("unrealizedProfit", 0.0) or 0.0)):
        pr = float(p.get("unrealizedProfit", 0.0) or 0.0)
        side = "شراء" if p.get("type") == "POSITION_TYPE_BUY" else "بيع"
        mark = "🟢" if pr >= 0 else "🔴"
        out.append(f"{mark} {side} `{p.get('volume')}` من `{p.get('openPrice')}` → "
                   f"`{pr:+.2f}$`")
    return out


async def released_watcher(get_conn, notify):
    """Report on trades the bot handed over. It never closes them."""
    global _released_summary_at
    # Start the clock now: the bot restarts often, and a summary fired on every boot is
    # noise. Events still report immediately, and /mine answers on demand.
    _released_summary_at = time.time()
    while True:
        try:
            conn = get_conn()
            ids = set(load().get("released", []))
            if conn is None or not ids:
                await asyncio.sleep(10)
                continue
            all_pos = await asyncio.wait_for(conn.get_positions(), timeout=8.0)
            mine = [p for p in all_pos if p.get("id") in ids]
            live = {p.get("id") for p in mine}

            gone = ids - live
            if gone:
                state = load()
                state["released"] = [i for i in state.get("released", []) if i in live]
                save(state)
                last = [f"`{_released_seen.pop(i, {}).get('last', 0.0):+.2f}$`" for i in gone]
                await notify(f"✅ *انغلقت {len(gone)} من صفقاتك المسلّمة.*\n"
                             f"آخر نتيجة شفتها عليها: {' · '.join(last)}\n"
                             "(أغلقتها إنت أو ضربت ستوب — أنا ما ألمسها.)")

            for p in mine:
                pid = p.get("id")
                pr = float(p.get("unrealizedProfit", 0.0) or 0.0)
                seen = _released_seen.setdefault(pid, {"worst": pr, "green": pr >= 0, "last": pr})
                seen["last"] = pr
                side = "الشراء" if p.get("type") == "POSITION_TYPE_BUY" else "البيع"
                if pr >= 0 and not seen["green"]:
                    seen["green"] = True
                    await notify(f"🎉 *صفقة {side} اللي عندك صارت رابحة* `{pr:+.2f}$`\n"
                                 f"• الدخول `{p.get('openPrice')}` · الحجم `{p.get('volume')}`\n"
                                 "هذي فرصتك تغلقها — القرار إلك، أنا ما ألمسها.")
                elif pr < 0:
                    seen["green"] = False
                    if pr <= seen["worst"] - GRID_RELEASED_STEP:
                        seen["worst"] = pr
                        await notify(f"⚠️ *صفقة {side} اللي عندك غاطت أكثر* `{pr:+.2f}$`\n"
                                     f"• الدخول `{p.get('openPrice')}` · الحجم `{p.get('volume')}`\n"
                                     "ما عندها وقف خسارة — انتبه لها.")

            now = time.time()
            if mine and now - _released_summary_at >= GRID_RELEASED_SUMMARY_MIN * 60:
                _released_summary_at = now
                total = pnl(mine)
                await notify("👁 *متابعة صفقاتك المسلّمة*\n\n" + "\n".join(released_lines(mine))
                             + f"\n\n*المجموع:* `{total:+.2f}$` — مفتوحة بإدارتك.")
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"released watcher error: {e}", exc_info=True)
            await asyncio.sleep(15)


async def released_text(conn):
    """What /mine shows."""
    ids = set(load().get("released", []))
    if not ids:
        return "🤝 ماكو صفقات مسلّمة — كل شي تحت إدارة البوت."
    if conn is None:
        return f"🤝 عندك `{len(ids)}` صفقة مسلّمة، بس ماكو اتصال حالياً لأجيب أسعارها."
    all_pos = await asyncio.wait_for(conn.get_positions(), timeout=8.0)
    mine = [p for p in all_pos if p.get("id") in ids]
    if not mine:
        return "🤝 صفقاتك المسلّمة كلها انغلقت."
    return ("🤝 *صفقاتك المسلّمة* — مفتوحة بإدارتك، البوت يراقب وما يتدخل\n\n"
            + "\n".join(released_lines(mine))
            + f"\n\n*المجموع:* `{pnl(mine):+.2f}$`\n"
            "⚠️ بلا وقف خسارة — إنت اللي تقرر متى تغلقها.")


async def refill_gaps(conn, symbol, positions, orders, notify=None, skip_magic=None,
                      room=None, quote=None):
    """Put a level back within one step of price when the nearest one has drifted away.

    Never touches an open position. Three things stop it feeding a trend: the per-side
    pending cap, `room` (how many more positions the grid may hold at all), and skip_magic,
    which is the side one-way has deliberately stood down - refilling that side was undoing
    the same decision on the next tick, over and over.
    """
    # Same rule as arm(): while a single-trade test is live, nothing tops the
    # ladder back up either. refill_gaps is the quieter of the two paths and
    # the easier one to forget.
    if safety.test_mode_active():
        return 0
    if not (GRID_REFILL and GRID_LEVELS > 0):
        return 0
    if room is not None and room <= 0:
        return 0
    try:
        if quote is None:
            quote = await quote_now(conn, symbol)
    except Exception as e:
        log.warning(f"refill skipped, no price: {e}")
        return 0
    bid, ask = float(quote.get("bid", 0) or 0), float(quote.get("ask", 0) or 0)
    if not bid or not ask:
        return 0
    lot = round(min(GRID_BASE_LOT, GRID_MAX_LOT), 2)
    dist = tp_distance(lot)
    gap = GRID_STEP * GRID_GAP_FACTOR
    added, recycled = 0, 0

    for magic, maker, ref, sign in (
            (MAGIC_GRID_BUY, conn.create_stop_buy_order, ask, 1),
            (MAGIC_GRID_SELL, conn.create_stop_sell_order, bid, -1)):
        if not bias_allows(magic):
            continue                      # the user stood this direction down by hand
        if magic == skip_magic:
            continue                      # the trend guard stood it down; leave it down
        if room is not None and added >= room:
            break
        side_orders = [o for o in orders if o.get("magic") == magic]
        levels = [float(o.get("openPrice", 0) or 0) for o in side_orders]
        nearest = min((abs(x - ref) for x in levels), default=None)
        if nearest is not None and nearest <= gap:
            continue                      # something is still close enough to catch price
        at = round(ref + sign * GRID_STEP, 2)
        if any(abs(at - x) < GRID_STEP / 2 for x in levels):
            continue                      # would sit on top of an existing level
        if len(side_orders) >= GRID_LEVELS:
            # The cap stops a trend turning this into an order machine, but refusing on a
            # full ladder left every level stranded far above price with nothing covering
            # it. Recycle instead: drop the furthest level to pay for the near one, so the
            # count never grows and the ladder walks toward price.
            furthest = max(side_orders, key=lambda o: abs(float(o.get("openPrice", 0) or 0) - ref))
            if abs(float(furthest.get("openPrice", 0) or 0) - ref) <= abs(at - ref):
                continue                  # nothing is further out than the level we want
            # Never remove a protective/working pending level unless its replacement
            # is permitted. In fail-closed mode, leave the existing ladder intact.
            try:
                require_entry_permission(stop_loss=entry_stop(at, sign > 0), lot=lot,
                                     conn=conn)
            except Exception as e:
                log.warning(f"refill blocked before recycling existing order: {e}")
                continue
            try:
                await asyncio.wait_for(conn.cancel_order(furthest["id"]), timeout=6.0)
                recycled += 1
            except Exception as e:
                log.warning(f"could not recycle {furthest.get('id')}: {e}")
                continue
        try:
            _opts = {"magic": magic}
            _tsl = server_trail()
            if _tsl:
                _opts["trailingStopLoss"] = _tsl
            require_entry_permission(stop_loss=entry_stop(at, sign > 0), lot=lot,
                                     conn=conn)
            res = await asyncio.wait_for(
                maker(symbol=symbol, volume=lot, open_price=at,
                      stop_loss=entry_stop(at, sign > 0),
                      take_profit=round(at + sign * dist, 2) if dist else None,
                      options=_opts), timeout=8.0)
            remember_order((res or {}).get("orderId"))
            added += 1
            log.info(f"refilled {'buy' if sign > 0 else 'sell'} level at {at}")
        except Exception as e:
            log.warning(f"refill at {at} failed: {e}")
    if added and notify:
        extra = (f"\nنقلت `{recycled}` أمر من أبعد نقطة بدل ما أزيد العدد." if recycled else "")
        await notify(f"🧩 *سدّيت فجوة بالشبكة* — أضفت `{added}` أمر قرب السعر الحالي، "
                     f"الصفقات المفتوحة ما لمستها.{extra}")
    return added


_halves = {}          # position id -> how many times it has already been halved


def halves_reset():
    _halves.clear()


def adverse_distance(p, bid, ask):
    """How far price has travelled AGAINST this position, in dollars. 0 if it is winning.

    Measured on the price that would close the trade - a buy closes at bid, a sell at ask -
    so the spread counts against it, exactly as the broker's own P/L does.
    """
    try:
        entry = float(p.get("openPrice", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if not entry:
        return 0.0
    if p.get("type") == "POSITION_TYPE_BUY":
        return max(0.0, entry - float(bid))
    return max(0.0, float(ask) - entry)


def half_volume(volume, min_lot=None):
    """Half of `volume`, rounded DOWN to the lot step, or None if it cannot be split.

    Rounding down keeps more of the trade open than rounding up would: on a split this
    tool should err toward doing less, not more.
    """
    min_lot = HALF_MIN_LOT if min_lot is None else min_lot
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return None
    if min_lot <= 0 or volume < min_lot * 2:
        return None                   # splitting it would leave less than one lot step
    step = min_lot
    cut = math.floor((volume / 2.0) / step) * step
    cut = round(cut, 2)
    if cut < min_lot or round(volume - cut, 2) < min_lot:
        return None
    return cut


async def halve_distant(conn, positions, bid, ask, notify=None):
    """Close half of any losing grid trade price has run HALF_DISTANCE against.

    Acts on at most ONE position per call and returns True when it did, so the caller can
    re-read the book instead of working from a list that is now wrong.
    """
    if not (HALF_ENABLED and HALF_DISTANCE > 0):
        return False
    if not hasattr(conn, "close_position_partially"):
        return False
    live = {p.get("id") for p in positions}
    for gone in [k for k in _halves if k not in live]:
        _halves.pop(gone, None)
    for p in positions:
        try:
            profit = float(p.get("unrealizedProfit", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if profit >= 0:
            continue                  # never cut a winner down
        pid = p.get("id")
        done = _halves.get(pid, 0)
        dist = adverse_distance(p, bid, ask)
        step = half_distance((float(bid) + float(ask)) / 2.0)
        if dist < step * (done + 1):
            continue                  # not another whole step away yet
        cut = half_volume(p.get("volume"))
        if cut is None:
            continue                  # already at the smallest tradable size
        try:
            await asyncio.wait_for(
                conn.close_position_partially(pid, cut), timeout=8.0)
        except Exception as e:
            log.warning(f"could not halve {pid}: {e}")
            continue                  # nothing recorded, so it is retried next tick
        _halves[pid] = done + 1
        left = round(float(p.get("volume", 0) or 0) - cut, 2)
        booked = round(profit * (cut / float(p.get("volume", 1) or 1)), 2)
        if notify:
            side = "شراء" if p.get("type") == "POSITION_TYPE_BUY" else "بيع"
            tail = ("" if half_volume(left) else
                    "\n• وصلت أصغر حجم — ما تنقسم بعد")
            await notify(
                f"✂️ *نصّفت صفقة مبتعدة*\n"
                f"• {side} من `{p.get('openPrice')}` — ابتعد عنها السعر `{dist:.2f}$`\n"
                f"• أغلقت `{cut:g}` بـ`{booked:g}$` · باقي `{left:g}`\n"
                f"• مخاطرتها انقصّت للنص{tail}")
        return True
    return False


# The broker's own rules for an instrument: contract size, digits, lot limits and the
# minimum distance between a stop and the market. Asked for once per symbol and kept -
# these are contract terms, not quotes.
#
# Keyed BY SYMBOL, and that is the whole point of this rewrite. The cache used to be a
# single pair of values for the entire bot, which was harmless while only gold was ever
# traded and silently wrong the moment a second instrument appeared: ask about gold,
# then ask about bitcoin, and bitcoin is handed gold's answer. Gold's contract is 100
# ounces and bitcoin's is 1 coin, so every dollar figure would have been out by a
# hundredfold - in the direction that understates the risk.
_specs = {}                       # symbol -> {"spec": {...}|None, "asked": float}
STOPS_RETRY_SECS = _f("STOPS_RETRY_SECS", 600.0)


def stops_reset():
    _specs.clear()


def points_to_price(points, digits):
    """A broker's stopsLevel is quoted in POINTS. This is the only place that converts.

    One point is one unit of the last decimal place, so 20 points on a 2-digit symbol
    is 0.20 of price. This function exists because the conversion was written twice in
    this file with two different formulas - one of them multiplied by a further 10 - and
    the two disagreed by an order of magnitude about the broker's minimum stop. Which
    copy you happened to read decided whether a break-even stop looked possible. That
    is the same "two owners of one rule" failure this project has already paid for
    three times, so there is now exactly one.
    """
    try:
        points, digits = float(points or 0), int(digits if digits is not None else 2)
    except (TypeError, ValueError):
        return 0.0
    if points <= 0:
        return 0.0
    return round(points * (10.0 ** -digits), 6)


def normalise_spec(sp):
    """The broker's specification reduced to the numbers we act on.

    `contract` is None when the broker did not say. None means unknown and callers must
    refuse to size an order on it - never fall back to a number, because the only
    number available to fall back to is gold's.
    """
    sp = sp or {}

    def _n(key):
        try:
            v = float(sp.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
        return v

    digits = int(_n("digits") or 2)
    cs = _n("contractSize")
    return {
        "symbol": sp.get("symbol") or "",
        "digits": digits,
        "contract": cs if cs > 0 else None,
        "min_lot": _n("minVolume"),
        "max_lot": _n("maxVolume"),
        "lot_step": _n("volumeStep"),
        "stop_points": _n("stopsLevel"),
        "gap": points_to_price(_n("stopsLevel"), digits),
    }


async def symbol_spec(conn, symbol, force=False):
    """The normalised spec for one symbol, or None when the broker has not answered.

    Cached per symbol so this costs one request per instrument per session, not one
    per tick. A failure is remembered too, briefly, so a broker that cannot answer is
    not hammered once a second.
    """
    key = str(symbol or "")
    hit = _specs.get(key)
    if hit and not force:
        if hit.get("spec") is not None:
            return hit["spec"]
        if time.time() - hit.get("asked", 0) < STOPS_RETRY_SECS:
            return None
    _specs[key] = {"spec": None, "asked": time.time()}
    if not hasattr(conn, "get_symbol_specification"):
        return None
    try:
        raw = await asyncio.wait_for(conn.get_symbol_specification(key), timeout=8.0)
    except Exception as e:
        log.warning(f"could not read the specification for {key}: {e}")
        return None
    spec = normalise_spec(raw)
    _specs[key] = {"spec": spec, "asked": time.time()}
    log.info(f"spec {key}: contract={spec['contract']} digits={spec['digits']} "
             f"stops={spec['stop_points']}pt = {spec['gap']} price "
             f"lot {spec['min_lot']}/{spec['lot_step']}/{spec['max_lot']}")
    return spec


def lot_problem(lot, spec):
    """Why this lot is not allowed for this instrument, or None when it is fine."""
    if not spec:
        return None
    try:
        lot = float(lot)
    except (TypeError, ValueError):
        return "اللوت مو رقم"
    mn, mx, st = spec.get("min_lot") or 0, spec.get("max_lot") or 0, spec.get("lot_step") or 0
    if mn and lot < mn - 1e-9:
        return f"اللوت `{lot:g}` أصغر من أقل لوت للوسيط `{mn:g}`"
    if mx and lot > mx + 1e-9:
        return f"اللوت `{lot:g}` أكبر من أقصى لوت للوسيط `{mx:g}`"
    if st and abs(round(lot / st) * st - lot) > 1e-6:
        return f"اللوت `{lot:g}` مو من مضاعفات خطوة الوسيط `{st:g}`"
    return None


async def why_not(conn, symbol, is_active=None, price=0.0):
    """Walk the engine's own decision chain and say which gate stopped it.

    Four separate evenings were spent inferring this from screenshots. Every gate the
    engine passes through is cheap to evaluate on demand, so the bot can simply be
    asked. Each line is the real condition, not a description of one.
    """
    L, blocker = [], None

    def row(ok, text, fix=""):
        nonlocal blocker
        L.append(("✅ " if ok else "❌ ") + text + (f"\n   ↳ {fix}" if not ok and fix else ""))
        if not ok and blocker is None:
            blocker = text

    row(GRID_ENABLED, "الشبكة مشغّلة (`GRID_ENABLED`)", "`/grid on`")
    if is_active is not None:
        row(bool(is_active), "النظام شغّال (`BOT_ACTIVE`)", "`/resume`")
    row(conn is not None, "أكو اتصال بالوسيط", "انتظر شوي أو `/check`")
    # Not an arming gate, but the one most likely to be the real complaint: a grid
    # that arms perfectly and protects nothing looks identical to a broken one.
    row(BE_ENABLED, "الحماية مشغّلة (`BE_ENABLED`) — وقف وتريلينغ وتأمين",
        "`/setkey beon 1`")
    row(True, f"الأداة `{GRID_SYMBOL}`")

    st = load()
    row(not st.get("halted"),
        f"الشبكة مو موقوفة" + (f" — موقوفة بسبب: `{st.get('halt_reason','؟')}`"
                               if st.get("halted") else ""), "`/grid on`")
    armed = bool(st.get("armed"))
    L.append(("ℹ️ السلّم منصوب أصلاً حول `%s` — ما ينصب ثاني" % st.get("center")) if armed
             else "ℹ️ السلّم مو منصوب — المحرك لازم ينصبه")

    if conn is None:
        return "🧭 *ليش ما ينصب*\n\n" + "\n".join(L)

    try:
        held, net, elsewhere = await book(conn, symbol)
        row(len(held) < GRID_MAX_POSITIONS,
            f"الصفقات المفتوحة `{len(held)}` تحت السقف `{GRID_MAX_POSITIONS}`",
            "سكّر صفقة أو ارفع `/setkey gridmaxpos`")
    except Exception as e:
        row(False, f"قراءة الصفقات: `{str(e)[:120]}`")
        held, elsewhere = [], 0.0

    try:
        acct = await asyncio.wait_for(conn.get_account_information(), timeout=6.0)
        bal = float(acct.get("balance", 0) or 0)
        eq = float(acct.get("equity", bal) or bal)
        down, lim, braked = drawdown_brake(bal, eq)
        fix = f"`/setkey brake {int(down / max(1.0, bal) * 100) + 5}` أو `0` للإلغاء"
        row(not braked,
            f"المكبح: نازل `{down:g}$` والحد `{lim:g}$` "
            f"(`{GRID_EQUITY_BRAKE_PCT:g}%` من `{bal:g}$`)"
            + (f" — `{abs(elsewhere):g}$` منه من أدوات ثانية" if elsewhere < -0.01 else ""),
            fix)
    except Exception as e:
        row(False, f"قراءة الحساب: `{str(e)[:120]}`")

    if price:
        ok_step, floor, nice = step_sane(price)
        row(ok_step, f"الخطوة `{GRID_STEP:g}$` فوق أرضيتها `{floor:g}$`",
            f"`/setkey gridstep {nice:g}`")

    head = ("🧭 *ليش ما ينصب*" if blocker else "🧭 *ماكو شي يمنع النصب*")
    tail = (f"\n\n🚧 *أول مانع:* {blocker}" if blocker else
            "\n\nكل البوابات مفتوحة. إذا لسه ماكو أوامر، `/grid rearm` وابعثلي الرد.")
    return head + "\n\n" + "\n".join(L) + tail


async def history_report(conn, symbol, days=30, lot=None, contract=None):
    """What the account ACTUALLY did, from the broker's own closed deals.

    Every test in this project checks that the code does what it was told. Not one
    of them says whether being told that makes money. A backtest would answer it with
    assumed fills and an assumed spread; the account's own history answers it with the
    fills that really happened, at this broker, on these settings. It is both faster
    to build and more truthful, so it comes first.
    """
    if not hasattr(conn, "get_deals_by_time_range"):
        return ("هذا الوضع ما يعطي تاريخ الصفقات (الاتصال مو RPC).\n"
                "جرّب بعد شوي أو `/check`.")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, int(days)))
    deals = []
    try:
        off = 0
        while True:
            page = await asyncio.wait_for(
                conn.get_deals_by_time_range(start, end, off, 1000), timeout=30.0)
            got = (page or {}).get("deals", page if isinstance(page, list) else [])
            deals.extend(got or [])
            if len(got or []) < 1000:
                break
            off += 1000
            if off >= 10000:
                break
    except Exception as e:
        return f"⚠️ ما كدرت أقرا التاريخ: `{str(e)[:220]}`"

    # Only what CLOSED a position, on this instrument. An entry deal carries no result.
    closed = [d for d in deals
              if (d.get("entryType") or "").upper().endswith("OUT")
              and (not symbol or d.get("symbol") == symbol)]
    if not closed:
        return (f"ماكو صفقات مغلقة على `{symbol}` بآخر `{days}` يوم.\n"
                f"(قرأت `{len(deals)}` حركة بالتاريخ)")

    def net(d):
        return (float(d.get("profit", 0) or 0) + float(d.get("commission", 0) or 0)
                + float(d.get("swap", 0) or 0))

    res = sorted((net(d) for d in closed))
    wins = [x for x in res if x > 0]
    losses = [x for x in res if x < 0]
    total = round(sum(res), 2)
    gross_w, gross_l = round(sum(wins), 2), round(-sum(losses), 2)
    pf = round(gross_w / gross_l, 2) if gross_l else None
    aw = round(gross_w / len(wins), 2) if wins else 0.0
    al = round(gross_l / len(losses), 2) if losses else 0.0
    rate = round(len(wins) / len(res) * 100, 1)
    need = round(al / (aw + al) * 100, 1) if (aw + al) else 0.0
    # Worst run of the equity curve built from these results, in order of closing.
    seq = [net(d) for d in sorted(closed, key=lambda d: str(d.get("brokerTime") or ""))]
    peak = run = worst = 0.0
    for x in seq:
        run += x
        peak = max(peak, run)
        worst = min(worst, run - peak)

    lot = float(lot or GRID_BASE_LOT)
    contract = float(contract or GRID_CONTRACT)
    fees = round(sum(abs(float(d.get("commission", 0) or 0))
                     + abs(float(d.get("swap", 0) or 0)) for d in closed), 2)

    verdict = ("🟢 موجب" if total > 0 and (pf or 0) > 1.2 else
               "🟡 حدّي" if total > 0 or (pf or 0) > 0.95 else "🔴 سالب")
    return "\n".join([
        f"📊 *أداء `{symbol}` الحقيقي — آخر `{days}` يوم*",
        f"*الحكم: {verdict}*",
        "",
        f"• صفقات مغلقة: `{len(res)}` · رابحة `{len(wins)}` · خاسرة `{len(losses)}`",
        f"• نسبة النجاح: `{rate}%` · *المطلوبة للتعادل: `{need}%`*",
        f"• متوسط الربح `{aw}$` · متوسط الخسارة `{al}$`",
        f"• إجمالي ربح `{gross_w}$` · إجمالي خسارة `{gross_l}$`",
        f"• عامل الربح: `{pf if pf is not None else '∞'}`"
        + ("  ← تحت 1 يعني خسارة" if pf is not None and pf < 1 else ""),
        f"• *الصافي: `{total:+g}$`*",
        f"• أسوأ ارتداد متتالي: `{worst:+.2f}$`",
        f"• عمولات وسواب: `{fees}$`",
        "",
        (f"⚠️ نسبة نجاحك `{rate}%` والمطلوب `{need}%` — "
         f"الفرق `{round(need - rate, 1)}` نقطة" if rate < need else
         f"✅ نسبة نجاحك فوق المطلوب بـ`{round(rate - need, 1)}` نقطة"),
    ])


async def reconcile_report(conn, symbol, magics=None):
    """What the bot believes, next to what the broker actually holds.

    Four separate defects in this project were the same shape: the bot could not
    see something, so it behaved as though it were not there. This asks the broker
    and prints the difference instead of assuming they agree.
    """
    try:
        orders = list(await asyncio.wait_for(conn.get_orders(), timeout=8.0) or [])
        positions = list(await asyncio.wait_for(conn.get_positions(), timeout=8.0) or [])
    except Exception as e:
        return f"⚠️ ما كدرت أقرا من الوسيط: `{str(e)[:200]}`"
    orders = [o for o in orders if o.get("symbol") == symbol]
    positions = [p for p in positions if p.get("symbol") == symbol]
    st = load()
    r = safety.reconcile(
        expected_order_ids=st.get("pending_ids", []),
        broker_orders=orders, broker_positions=positions,
        magics=tuple(magics or MAGICS), trigger_cash=BE_TRIGGER_CASH,
        adopted=st.get("adopted", []), released=st.get("released", []),
        tolerance=max(GRID_STEP * 0.5, 0.01))
    out = [f"🧮 *مطابقة `{symbol}`*",
           await account_line(conn),
           f"عند الوسيط: `{len(orders)}` أمر معلّق · `{len(positions)}` صفقة مفتوحة"]
    rows = []
    if r["vanished"]:
        rows.append(f"❌ `{len(r['vanished'])}` أمر أحسبه موجود وماكو عند الوسيط")
    if r["unexpected"]:
        rows.append(f"❓ `{len(r['unexpected'])}` أمر بماغيك البوت ما أتابعه")
    if r["unprotected"]:
        ids = ", ".join(f"`{p.get('id')}`" for p in r["unprotected"][:5])
        rows.append(f"🚨 `{len(r['unprotected'])}` صفقة رابحة بلا وقف حقيقي: {ids}")
    if r["duplicates"]:
        rows.append(f"♻️ `{len(r['duplicates'])}` مستوى مكرر (شبكة منصوبة مرتين)")
    if r["foreign"]:
        rows.append(f"👤 `{len(r['foreign'])}` صفقة مو من البوت — ما يلمسها (`/trades`)")
    out.append("\n".join(rows) if rows
               else "✅ كل شي مطابق — ماكو فرق بين اللي أحسبه واللي عند الوسيط.")
    return "\n\n".join(out)


async def account_line(conn):
    """Name the account being read, so a mismatch with the user's terminal shows up.

    Every 'nothing there' answer in this project has at some point meant 'I was
    looking somewhere else'. An account number costs one cached read and removes
    the whole category of that mistake.
    """
    try:
        acct = await asyncio.wait_for(conn.get_account_information(), timeout=6.0)
    except Exception as e:
        return f"⚠️ ما كدرت أقرا هوية الحساب: `{str(e)[:120]}`"
    login = acct.get("login") or acct.get("id") or "؟"
    srv = acct.get("server") or acct.get("broker") or "؟"
    bal = acct.get("balance", 0) or 0
    eq = acct.get("equity", bal) or bal
    return (f"🏦 الحساب `{login}` · `{srv}` · رصيد `{float(bal):,.2f}$` "
            f"· حقوق `{float(eq):,.2f}$`\n"
            f"_قارنه برقم حسابك بالـMT5 — لازم يكون نفسه._")


async def trades_report(conn, symbol, bid=0.0, ask=0.0):
    """Every open position: who owns it, what protects it, and what acts next.

    Written because a winning trade sat on the chart with no stop on it and neither of
    us could say why from a screenshot. Guessing cost hours; this answers in one line
    per trade, from the broker's own data.
    """
    try:
        pos = await asyncio.wait_for(conn.get_positions(), timeout=8.0)
    except Exception as e:
        return f"⚠️ ما كدرت أقرا الصفقات: `{str(e)[:200]}`"
    all_pos = list(pos or [])
    # WHICH account is this? The report said "no open trades" without ever naming the
    # account it had looked at, so "I am reading a different account from the one on
    # your screen" and "you have no trades" printed the same sentence.
    head = [f"📋 *صفقات `{symbol}`*", await account_line(conn)]
    pos = [p for p in all_pos if p.get("symbol") == symbol]
    if not pos:
        # And say what the account DOES hold. A symbol spelled XAUUSD. or GOLD on this
        # broker would be filtered out above and read, wrongly, as an empty account.
        other = {}
        for p in all_pos:
            other[p.get("symbol")] = other.get(p.get("symbol"), 0) + 1
        rest = ("\n".join(f"• `{k}` — `{v}` صفقة" for k, v in sorted(other.items()))
                if other else "")
        tail = (f"\n\n⚠️ *بس الحساب بيه صفقات على رموز ثانية:*\n{rest}\n"
                f"إذا واحد منها هو نفس الذهب باسم ثاني، `/asset <الاسم>` يحوّل البوت له."
                if other else "\n\nوالحساب فاضي تماماً — ماكو ولا صفقة على أي رمز.")
        return "\n".join(head) + f"\n\nماكو صفقات مفتوحة على `{symbol}`." + tail
    st = load()
    rel = set(st.get("released", []))
    ado = {str(i) for i in st.get("adopted", [])}
    out = list(head)
    for p in pos:
        pid, mg = p.get("id"), p.get("magic")
        lot = float(p.get("volume", 0) or 0)
        entry = float(p.get("openPrice", 0) or 0)
        try:
            pl = float(p.get("unrealizedProfit", 0) or 0)
        except (TypeError, ValueError):
            pl = 0.0
        is_buy = p.get("type") == "POSITION_TYPE_BUY"
        side = "شراء" if is_buy else "بيع"
        if pid in rel:
            who = "🤝 مسلّمة إلك — البوت ما يلمسها"
        elif str(pid) in ado:
            who = "🫱 متبنّاة — البوت يحميها (`/unadopt` يرجّعها)"
        elif mg in MAGICS:
            who = "🕸 شبكة — البوت يديرها"
        elif mg in (777111, 777222):
            who = "⚓ ارتكاز — البوت يديرها"
        else:
            who = (f"👤 مو من البوت (magic `{mg}`) — ما يلمسها\n"
                   f"  `/adopt {pid}` يخليه يحميها")
        sl = p.get("stopLoss")
        tp = p.get("takeProfit")
        trail = "✅" if has_server_trail(p) else "❌"
        cur = (f"✅" if trail == "✅" and trail_is_current(p, server_trail()) else
               ("قديم ⚠️" if trail == "✅" else "❌"))
        lines = [f"\n*{side} `{lot:g}` @ `{entry}`* · `{pl:+.2f}$` · رقمها `{pid}`",
                 f"  {who}",
                 f"  وقف: `{sl if sl else 'ماكو'}` · هدف: `{tp if tp else 'ماكو'}` "
                 f"· تريلينغ الوسيط: {cur}"]
        if (mg in MAGICS or str(pid) in ado) and not (pid in rel):
            nxt = []
            if BE_ENABLED and pl < BE_TRIGGER_CASH:
                need = round((BE_TRIGGER_CASH - pl) / max(1e-9, lot * GRID_CONTRACT), 2)
                nxt.append(f"التأمين بعد `{need}$` حركة")
            elif BE_ENABLED:
                nxt.append(f"مؤمّنة — الوقف ما ينزل تحت `{BE_LOCK_CASH:g}$`")
            if TRADE_GIVEBACK_PCT > 0:
                pk = max(peak_of(pid), pl)
                fl = giveback_floor(pk)
                if fl is not None and pk >= BE_LOCK_CASH:
                    nxt.append(f"ارتداد يغلقها عند `{fl:g}$` (القمة `{pk:g}$`)")
            if GRID_TP_PER_TRADE > 0:
                nxt.append(f"هدف `{GRID_TP_PER_TRADE:g}$`")
            if nxt:
                lines.append("  الجاي: " + " · ".join(nxt))
        out.append("\n".join(lines))
    # Positions on OTHER instruments. The bot filters by symbol, so a trade left
    # behind by an instrument switch - a BTCUSD position still open after the bot
    # went back to gold - is managed by nobody and mentioned nowhere. It only ever
    # surfaced inside the equity-brake message, which fires when it is already bad.
    foreign = [p for p in all_pos if p.get("symbol") != symbol]
    if foreign:
        tot = round(sum(float(p.get("unrealizedProfit", 0) or 0) for p in foreign), 2)
        out.append(f"\n\n🧭 *وأدوات ثانية بالحساب* — `{tot:+.2f}$`")
        for p in sorted(foreign, key=lambda q: float(q.get("unrealizedProfit", 0) or 0)):
            sd = "شراء" if p.get("type") == "POSITION_TYPE_BUY" else "بيع"
            out.append(f"• `{p.get('symbol')}` {sd} `{float(p.get('volume', 0) or 0):g}` "
                       f"· `{float(p.get('unrealizedProfit', 0) or 0):+.2f}$` — "
                       f"خارج إدارة البوت تماماً")
        out.append("_البوت يشتغل على رمز واحد؛ هاي ما ينطيها وقف ولا يغلقها._")
    out.append(f"\n⚙️ لوت `{GRID_BASE_LOT:g}` · عقد `{GRID_CONTRACT:g}` · "
               f"كل `1$` ربح = حركة `{cash_in_price(1.0):g}$`")
    return "\n".join(out)


async def spec_report(conn, symbol, price=0.0):
    """Ask the broker its own rules for this symbol and check our settings against them.

    Written after a third round of guessing at why not one order would place on
    bitcoin: minimum lot? lot step? stops level? Every one of those answers lives in
    the symbol specification, one call away, and we were reading screenshots instead.
    """
    if not hasattr(conn, "get_symbol_specification"):
        return "هذا الوسيط ما يعطي مواصفات الأداة."
    sp = await symbol_spec(conn, symbol, force=True)
    if sp is None:
        return f"⚠️ ما كدرت أجيب مواصفات `{symbol}`."
    mn, mx, st = sp["min_lot"], sp["max_lot"], sp["lot_step"]
    cs = sp["contract"] or 0.0
    dg = sp["digits"]
    lvl = sp["stop_points"]
    gap = sp["gap"]

    lots = ladder(base=recovery_base(load().get("loss_streak", 0)))
    lot = lots[0] if lots else GRID_BASE_LOT
    bad = []
    if mn and lot < mn - 1e-9:
        bad.append(f"❌ لوتك `{lot:g}` أصغر من حد الوسيط `{mn:g}` — "
                   f"`/setkey gridlot {mn:g}` و `/setkey gridmax {mn:g}`")
    if st and abs(round(lot / st) * st - lot) > 1e-6:
        bad.append(f"❌ لوتك `{lot:g}` مو من مضاعفات خطوة الوسيط `{st:g}`")
    if mx and lot > mx + 1e-9:
        bad.append(f"❌ لوتك `{lot:g}` أكبر من حد الوسيط `{mx:g}`")
    if cs and abs(cs - GRID_CONTRACT) > 1e-9:
        bad.append(f"❌ العقد عندك `{GRID_CONTRACT:g}` والوسيط يقول `{cs:g}` — "
                   f"`/setkey gridcontract {cs:g}`")
    if gap and GRID_STOP_DIST > 0 and GRID_STOP_DIST < gap:
        bad.append(f"❌ وقفك الثابت `{GRID_STOP_DIST:g}$` أقرب من حد الوسيط "
                   f"`{gap:g}$` — `/setkey gridstop 0`")
    if gap and BE_ENABLED and lot and GRID_CONTRACT:
        trail_px = BE_TRAIL_CASH / (lot * GRID_CONTRACT) if BE_TRAIL_CASH > 0 else 0.0
        if trail_px and trail_px < gap:
            bad.append(f"❌ تريلينغ `{BE_TRAIL_CASH:g}$` = `{trail_px:.2f}$` سعر، "
                       f"أقرب من حد الوسيط `{gap:g}$` — "
                       f"`/setkey betrail {round(gap * lot * GRID_CONTRACT * 1.5, 2):g}`")
    if price:
        ok_step, floor, nice = step_sane(price)
        if not ok_step:
            bad.append(f"❌ الخطوة `{GRID_STEP:g}$` أصغر من `{floor:g}$` — "
                       f"`/setkey gridstep {nice:g}`")

    return ("\n".join([
        f"🔎 *قواعد الوسيط لـ* `{symbol}`",
        f"• أقل لوت `{mn:g}` · خطوة اللوت `{st:g}` · أقصى `{mx:g}`",
        f"• حجم العقد `{cs:g}` · الخانات `{dg}` · "
        f"أقرب وقف `{lvl:g}` نقطة = `{gap:g}$`",
        "",
        f"⚙️ *إعداداتك:* لوت `{lot:g}` · عقد `{GRID_CONTRACT:g}` · "
        f"خطوة `{GRID_STEP:g}$` · وقف ثابت `{GRID_STOP_DIST:g}$`",
        ""] + (bad if bad else ["✅ كل الإعدادات متوافقة مع الوسيط."])))


async def broker_stop_gap(conn, symbol):
    """Smallest gap the broker allows between a stop and the market, in price units.

    Now a thin read over symbol_spec, which owns both the request and the points
    conversion. Returns None when the broker has not told us, and the caller then keeps
    its conservative behaviour rather than guessing.
    """
    spec = await symbol_spec(conn, symbol)
    return None if spec is None else spec["gap"]


# --- the owner's rule, handed to the server instead of polled for -------------------
# Everything above this line protects a trade by LOOKING at it: read the price, read the
# position, decide, write a new stop. That is a loop, and a loop needs the API to be
# answering - which is exactly what it was not, for most of today.
#
# MetaApi takes the whole rule as data and runs it itself. One write per trade and the
# protection is server-side: it holds when the bot is off, when the VM is down, when the
# quota is spent, when the phone is in a drawer. The polling guard becomes a backstop
# rather than the thing the money depends on.
#
# The rule, in his words, mapped to MetaApi's own fields:
#   "مجرد ما تدخل بالربح خلي وقف عند نقطة الدخول"  -> threshold 0.35 -> stopLoss 0.10
#   "وبعدها تمشي معاها بالربح"                      -> distance 0.50 behind
#   "تعدّي 2 دولار ما ينزل الوقف تحتها"              -> threshold 2.00 -> stopLoss 2.00
# RELATIVE_CURRENCY means the numbers are DOLLARS of profit, not price - so one config
# is correct at 0.01 lots and at 0.05 lots with no arithmetic on our side.
SERVER_TRAIL = os.environ.get("SERVER_TRAIL", "1") not in ("0", "false", "no")

# A hard stop, in DOLLARS OF PRICE, written onto the order when it is placed. 0 = none.
#
# The owner asked for this with a reason worth recording: he is already deep on one side
# and does not want the hedge turning into a second pile of stranded trades. A sell that
# does not work within a dollar is to be cut, not carried. That is a different thing from
# the per-trade stop he turned down earlier, which would have been applied to everything.
#
# Two properties matter. It goes on the ORDER, so the broker holds it - no polling, works
# while the API is throttled. And it is NEVER applied to a position that is already open:
# writing a $1 stop onto trades that are $20 under water would shut them all at once, and
# nothing he has asked for means that.
GRID_STOP_DIST = _f("GRID_STOP_DIST", 0.0)

# A grid step is only meaningful relative to what the instrument costs. $2 on gold near
# $4,000 is 0.05% - twelve levels span $24, which gold takes hours to cross. Carry that
# same $2 to bitcoin near $100,000 and the whole twelve-level ladder spans 0.024% of
# price: every level fills inside a minute at effectively one price, and what is left is
# not a grid but one oversized position with no plan. The floor below is deliberately
# under gold's own ratio, so an existing gold setup keeps working untouched, while a
# step that cannot work on a dearer instrument is refused with the number to use.
GRID_STEP_MIN_PCT = _f("GRID_STEP_MIN_PCT", 0.02)     # of price, per step
GRID_SYMBOL = os.environ.get("SYMBOL", "GOLD").strip()


def set_symbol(sym):
    """One source of truth for which instrument the grid owns.

    The engine receives the symbol as an argument while the filters read it
    independently from the environment. Two reads of the same setting agree right up
    until they do not - a caller that passes "XAUUSD" while the environment says
    "GOLD" makes every grid position invisible, and the grid silently owns nothing.
    The engine now publishes the symbol it was actually given.
    """
    global GRID_SYMBOL
    if sym and str(sym).strip() and str(sym).strip() != GRID_SYMBOL:
        GRID_SYMBOL = str(sym).strip()
        log.info(f"grid symbol set to {GRID_SYMBOL}")
# What a lot is, in words, so the panel can say it instead of printing a bare 100.
UNIT_WORD = {100.0: "100 أونصة", 1.0: "وحدة واحدة", 1000.0: "1000 وحدة"}


def step_floor(price):
    """Smallest step that still makes a grid on an instrument at this price."""
    if GRID_STEP_MIN_PCT <= 0 or not price:
        return 0.0
    return round(float(price) * GRID_STEP_MIN_PCT / 100.0, 2)


def step_sane(price):
    """(ok, floor, suggested). `suggested` is the gold-equivalent ratio, 0.05% of price."""
    floor = step_floor(price)
    return (GRID_STEP >= floor, floor, round(float(price) * 0.05 / 100.0, 2) if price else 0.0)


def entry_stop(price_at, is_buy):
    """The protective stop for a new order at `price_at`, or None when switched off."""
    if GRID_STOP_DIST <= 0:
        return None
    return round(price_at - GRID_STOP_DIST, 2) if is_buy else round(price_at + GRID_STOP_DIST, 2)


# How far past the floor the price must go before the floor's stop is placed. Without
# it the stop lands on top of the market and the trade dies on noise.
FLOOR_HEADROOM_CASH = _f("FLOOR_HEADROOM_CASH", 0.0) or None


def _floor_headroom():
    """A quarter of the floor, and nothing cleverer.

    The first version took max(quarter, BE_TRIGGER_CASH), which made the arming point
    depend on an unrelated setting: with a $2 floor and a $1.20 trigger it armed at
    $3.20, not the $2.50 the owner had been told. A protection rule he cannot predict
    from his own numbers is a rule he cannot trust.
    """
    return FLOOR_HEADROOM_CASH if FLOOR_HEADROOM_CASH else max(0.25 * BE_FLOOR_CASH, 0.05)


def server_trail():
    """His protection rule as a MetaApi trailingStopLoss, or None if switched off."""
    if not (SERVER_TRAIL and BE_ENABLED):
        return None
    steps = []
    if BE_TRIGGER_CASH > 0:
        steps.append({"threshold": round(BE_TRIGGER_CASH, 2),
                      "stopLoss": round(BE_LOCK_CASH, 2)})
    if BE_FLOOR_CASH > 0 and BE_FLOOR_CASH > BE_LOCK_CASH:
        # The floor used to trigger at the very figure it locked: "at $2 profit, keep
        # $2" puts the stop exactly where price is, so the first tick against it closes
        # the trade at the instant it earned the protection. The floor still LOCKS what
        # it says; it just waits for a little headroom before arming, so there is room
        # between the stop and the market.
        steps.append({"threshold": round(BE_FLOOR_CASH + _floor_headroom(), 2),
                      "stopLoss": round(BE_FLOOR_CASH, 2)})
    cfg = {}
    if steps:
        cfg["threshold"] = {"thresholds": steps,
                            "units": "RELATIVE_CURRENCY",
                            "stopPriceBase": "OPEN_PRICE"}
    if BE_TRAIL_CASH > 0:
        # MetaApi takes whichever of distance and threshold ends up CLOSEST to price,
        # so the trail tightens past the floor and the floor holds under it. That is
        # the rule as he stated it, with no extra logic from us.
        cfg["distance"] = {"distance": round(BE_TRAIL_CASH, 2),
                           "units": "RELATIVE_CURRENCY"}
    return cfg or None


def has_server_trail(p):
    tsl = (p or {}).get("trailingStopLoss")
    return bool(tsl and (tsl.get("threshold") or tsl.get("distance")))


def _trail_shape(tsl):
    """The numbers that define a trail, so two of them can be compared."""
    # A broker can hand back any shape at all, including a plain string where a dict
    # is expected. Anything unreadable returns None, which the caller treats as
    # "leave it alone" rather than rewriting the position on every tick.
    try:
        tsl = tsl or {}
        th = (tsl.get("threshold") or {}).get("thresholds") or []
        steps = tuple(sorted((round(float(x.get("threshold", 0)), 2),
                              round(float(x.get("stopLoss", 0)), 2)) for x in th))
        d = (tsl.get("distance") or {}).get("distance")
        return steps, (round(float(d), 2) if d is not None else None)
    except (TypeError, ValueError, AttributeError, KeyError):
        return None


def trail_is_current(p, cfg):
    """True when the position already carries THIS rule, not merely some rule.

    has_server_trail only ever asked "is there a trail?". So the moment the owner
    changed his protection - from locking $0.10 to locking $1.00 - every position
    already running kept the old rule forever, silently, because something was there.
    A rule he can change is a rule that has to be pushed when it changes.
    """
    have = _trail_shape((p or {}).get("trailingStopLoss"))
    want = _trail_shape(cfg)
    if have is None or want is None:
        return has_server_trail(p)      # cannot compare: leave it rather than thrash
    return have == want


async def ensure_server_trail(conn, positions, notify=None):
    """Hand the rule to the server for any position that is not already carrying it.

    One write each, once in the life of the position. After this the trade is protected
    by MetaApi whether or not this process is alive.
    """
    cfg = server_trail()
    if not cfg or not hasattr(conn, "modify_position"):
        return 0
    done, upgraded = 0, 0
    for p in positions:
        if trail_is_current(p, cfg):
            continue
        if has_server_trail(p):
            upgraded += 1
        try:
            await asyncio.wait_for(
                conn.modify_position(p["id"], trailing_stop_loss=cfg), timeout=8.0)
            done += 1
        except Exception as e:
            log.warning(f"server trail refused on {p.get('id')}: {e}")
    if done and notify:
        floor = (f" ولا ينزل تحت `{BE_FLOOR_CASH:g}$` بعد ما تعديها"
                 if BE_FLOOR_CASH > 0 else "")
        if upgraded:
            await notify(f"♻️ *حدّثت الحماية على `{upgraded}` صفقة شغالة* "
                         f"— كانت على القاعدة القديمة.")
        await notify(
            f"🔐 *سلّمت حراسة `{done}` صفقة للسيرفر*\n"
            f"• أول ما تربح `{BE_TRIGGER_CASH:g}$` ينتقل وقفها لنقطة الدخول\n"
            f"• وبعدها يمشي وراها على بُعد `{BE_TRAIL_CASH:g}$`{floor}\n"
            f"• *ينفّذها MetaApi نفسه* — تشتغل حتى لو البوت مطفي أو السيرفر واقف "
            f"أو الحصة منتهية")
    return done


# Give back at most this share of a trade's best profit before it is taken. 0 = off.
# The broker's own trailing stop can only hold a FIXED distance; a share of the peak
# has to be measured here, tick by tick. So the two work together and neither is
# redundant: this one is tighter and acts first while the bot is alive, and the
# server-side lock underneath it still holds if the bot is not.
# The owner's rule, in his words: once a trade is +1.50$ up, remember its best, and
# close it when it gives back a tenth of that best. It shipped working, then stopped,
# and the reason was never one bug. It was THREE implementations of one idea - this
# one, the anchor loop in range_harvester, and a MetaApi trailing rule - each with its
# own threshold, its own switch and its own set of magic numbers, so turning any of
# them on left the other two off and the behaviour depended on which trade you had.
# This is now the only one that closes on a give-back.
#
# It defaulted to 0.0, which means OFF. A protection whose default is "do nothing"
# will be off on every machine where nobody remembered to set it, which is every
# machine.
TRADE_GIVEBACK_PCT = _f("TRADE_GIVEBACK_PCT", 10.0)

# The profit at which the trailing memory starts. Deliberately its own setting and
# not BE_LOCK_CASH: the old code armed on the amount it LOCKS, which is a different
# question from the amount at which it starts WATCHING, and 0.10$ meant it armed on
# noise.
GIVEBACK_ARM_CASH = _f("GIVEBACK_ARM_CASH", 1.50)


def giveback_floor(peak):
    """The profit at which a trade that peaked at `peak` is taken: peak x 0.90.

    No floor under it any more. The old version returned max(peak*0.9, BE_LOCK_CASH),
    so whenever the lock was the larger of the two the trade closed at the lock
    instead of at the give-back - the rule the owner asked for, silently replaced by
    a different one whenever his settings happened to cross.
    """
    if TRADE_GIVEBACK_PCT <= 0 or peak <= 0:
        return None
    return round(peak * (1.0 - TRADE_GIVEBACK_PCT / 100.0), 2)


async def take_giveback(conn, positions, notify=None, allowed=True):
    """Close any trade that has handed back more than its share of its own best.

    NOT gated on the system switch, and that is the correction. /pause is documented
    to the owner as "freeze the bot WITHOUT closing anything - the guard keeps
    running", and then the guard was wired to stop with everything else. Refusing to
    open is a different decision from refusing to protect what is already open, and
    conflating them meant every pause quietly removed the protection from live money.

    `allowed` is still accepted so existing callers keep working, and still blocks
    the rest, but it no longer blocks THIS: there is no state of the system in which
    letting a won trade round-trip is the safer answer.

    This is bot-side, not broker-side. It depends on this process being alive and on
    MetaApi answering, and the close goes out at market, so what is actually realised
    can be below the trigger. It is not a guarantee and must not be described as one;
    the broker-side stop is the thing that holds when this does not.
    """
    if TRADE_GIVEBACK_PCT <= 0 or not hasattr(conn, "close_position"):
        return 0
    taken, failed = [], []
    for p in positions:
        pid = p.get("id")
        try:
            profit = float(p.get("unrealizedProfit", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        peak = note_peak(pid, profit)
        if peak < GIVEBACK_ARM_CASH:
            continue                      # never reached the arming profit
        floor = giveback_floor(peak)
        if floor is None or profit > floor:
            continue
        try:
            await asyncio.wait_for(conn.close_position(pid), timeout=8.0)
        except Exception as e:
            # An error, or a timeout, is NOT a close. Say so and leave the peak in
            # place so the next tick tries again on the same trade, rather than
            # reporting a harvest that never happened.
            failed.append((pid, str(e)[:120]))
            log.warning(f"give-back close refused on {pid}: {e}")
            continue
        taken.append((pid, peak, profit))
        forget_peak(pid)
        # Drop it from the caller's list too. Everything after this in the pass -
        # the trail refresh, the break-even move - runs on the SAME list, and a
        # position that was closed a line ago is still in it. apply_breakeven then
        # called note_peak on the dead trade and wrote its peak straight back, so a
        # harvested trade left a peak behind for an id that no longer exists, and
        # the next ticket to reuse that number would inherit it.
        positions[:] = [q for q in positions if q.get("id") != pid]
    if failed and notify:
        lines = "\n".join(f"\u2022 `{i}` \u2014 `{w}`" for i, w in failed)
        await notify("\U0001f6a8 *\u0645\u0627 \u0643\u062f\u0631\u062a \u0623\u063a\u0644\u0642 \u0639\u0646\u062f \u0627\u0644\u0627\u0631\u062a\u062f\u0627\u062f*\n" + lines +
                     "\n\u2022 \u0631\u0627\u062d \u0623\u0639\u064a\u062f \u0627\u0644\u0645\u062d\u0627\u0648\u0644\u0629 \u2014 \u0648\u0625\u0630\u0627 \u0645\u0633\u062a\u0639\u062c\u0644 \u0633\u0643\u0651\u0631\u0647\u0627 \u0645\u0646 MT5")
    if taken and notify:
        tot = round(sum(x[2] for x in taken), 2)
        lines = "\n".join(f"\u2022 \u0642\u0645\u062a\u0647\u0627 `{pk:g}$` \u2190 \u0623\u063a\u0644\u0642\u062a\u0647\u0627 \u0639\u0644\u0649 `{pr:g}$`"
                          for _i, pk, pr in taken)
        await notify(f"\U0001f3af *\u062d\u0635\u062f\u062a `{len(taken)}` \u0635\u0641\u0642\u0629 \u0639\u0644\u0649 \u0627\u0631\u062a\u062f\u0627\u062f "
                     f"`{TRADE_GIVEBACK_PCT:g}%`* \u2014 \u0627\u0644\u0645\u062c\u0645\u0648\u0639 `{tot:+g}$`\n{lines}")
    return len(taken)


_be_peaks = {}        # best profit each secured position has shown, by str(id)
_be_waiting = {}      # id -> True once the user has been told this one cannot be stopped yet
_peaks_loaded = False
_peaks_dirty = False
_peaks_saved_at = 0.0
PEAKS_SAVE_SECS = 20.0


def _load_peaks():
    """Bring the remembered peaks back after a restart.

    Three separate protections are measured from the best a trade has shown:
    the give-back exit, the break-even lock, and the soft exit. All three lived in
    a dict that died with the process - so every `/update`, every crash and every
    reconnect silently reset them. A trade that had run to +5$ came back with a
    peak of whatever it happened to be worth at that second, and soft_protect,
    which skips anything whose peak never reached the trigger, stopped guarding it
    altogether. The owner was told to run /update several times a day.
    """
    global _peaks_loaded
    if _peaks_loaded:
        return
    _peaks_loaded = True
    try:
        for k, v in (load().get("peaks") or {}).items():
            _be_peaks[str(k)] = float(v)
    except Exception as e:
        log.warning(f"could not restore peaks: {e}")


def note_peak(pid, profit):
    """Record and return the best this position has ever shown."""
    global _peaks_dirty
    _load_peaks()
    key = str(pid)
    try:
        profit = float(profit)
    except (TypeError, ValueError):
        profit = 0.0
    peak = max(_be_peaks.get(key, 0.0), profit)
    # A trade that has never been in profit gets no entry: storing a peak of zero for
    # every loser would grow the file and mark it dirty on every single tick.
    if peak > 0 and peak > _be_peaks.get(key, 0.0):
        _be_peaks[key] = peak
        _peaks_dirty = True
        # Once a peak is high enough to decide a close, it goes to disk NOW rather
        # than waiting out the debounce. The twenty-second window was twenty seconds
        # in which a crash threw away the number the give-back exit is measured from,
        # and the exit then used whatever the trade happened to be worth on restart -
        # which is lower, every time, because that is why it restarted mattering.
        if peak >= GIVEBACK_ARM_CASH:
            flush_peaks()
    return peak


def flush_peaks():
    """Write the peaks out now. Safe to call often; it only writes when something moved."""
    global _peaks_dirty, _peaks_saved_at
    if not _peaks_dirty:
        return
    try:
        state = load()
        state["peaks"] = {k: round(v, 2) for k, v in _be_peaks.items()}
        save(state)
        _peaks_dirty, _peaks_saved_at = False, time.time()
    except Exception as e:
        log.warning(f"could not persist peaks: {e}")


def peak_of(pid):
    _load_peaks()
    return _be_peaks.get(str(pid), 0.0)


def forget_peak(pid):
    global _peaks_dirty
    _load_peaks()
    if _be_peaks.pop(str(pid), None) is not None:
        _peaks_dirty = True


def prune_peaks(live_ids):
    """Drop peaks whose position is gone, and write the rest to disk.

    Debounced: the peaks move on most ticks and the state file is small but not
    free. A peak that is at most PEAKS_SAVE_SECS stale costs nothing, because the
    in-memory copy is what every decision reads.
    """
    global _peaks_dirty, _peaks_saved_at
    _load_peaks()
    live = {str(i) for i in live_ids}
    removed = [k for k in _be_peaks if k not in live]
    for gone in removed:
        _be_peaks.pop(gone, None)
        _peaks_dirty = True
    now = time.time()
    # A rising peak can wait out the debounce; a position that has CLOSED cannot.
    # Leaving its peak on disk means a restart inside the window reloads a peak for
    # a trade that no longer exists, and the next ticket to reuse that id inherits it.
    if _peaks_dirty and (removed or now - _peaks_saved_at >= PEAKS_SAVE_SECS):
        state = load()
        state["peaks"] = {k: round(v, 2) for k, v in _be_peaks.items()}
        save(state)
        _peaks_dirty, _peaks_saved_at = False, now


def has_protective_stop(p, spread):
    """True when this trade already carries a stop at or better than break-even."""
    try:
        sl = float(p.get("stopLoss") or 0)
        entry = float(p.get("openPrice", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not sl or not entry:
        return False
    if p.get("type") == "POSITION_TYPE_BUY":
        return sl >= round(entry + spread, 2) - 0.001
    return sl <= round(entry - spread, 2) + 0.001


async def soft_protect(conn, positions, bid, ask, notify=None, allowed=True):
    """Close a trade that showed profit, has no placeable stop, and gave the profit back.

    This is the only thing that can cover the window below `spread + gap` above entry,
    where any broker stop would sit under break-even. It closes at about zero rather than
    letting a trade that was up turn red - the rule the user has stated more than once.

    `allowed` is the system switch. The stop-tightening around it is safe in every state
    and always runs; CLOSING is an action, so /pause must be able to stop it.
    """
    if not (allowed and BE_ENABLED and BE_SOFT):
        return 0
    spread = max(0.0, float(ask) - float(bid))
    closed = []
    for p in positions:
        pid = p.get("id")
        peak = peak_of(pid)
        if peak < BE_TRIGGER_CASH:
            continue                      # never showed real profit; nothing to defend
        if has_protective_stop(p, spread):
            continue                      # the broker is holding it; leave it alone
        try:
            profit = float(p.get("unrealizedProfit", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if profit > BE_SOFT_EXIT:
            continue                      # still in profit; let it run
        try:
            await asyncio.wait_for(conn.close_position(pid), timeout=8.0)
        except Exception as e:
            log.warning(f"soft protect could not close {pid}: {e}")
            continue
        closed.append((pid, peak, profit))
        forget_peak(pid)
        _be_waiting.pop(pid, None)
    if closed and notify:
        for pid, peak, profit in closed:
            await notify(
                f"🛟 *أغلقتها على التعادل*\n"
                f"• كانت رابحة `{peak:g}$` ورجعت `{profit:g}$`\n"
                f"• ما كان ينفع ستوب عند الوسيط بعد — السعر قريب من الدخول\n"
                f"• فحرستها بنفسي وما خليتها تصير خسارة")
    return len(closed)


async def apply_breakeven(conn, positions, bid, ask, notify=None, label="", gap_floor=None):
    """Move the stop of any position past the trigger to its entry, costs covered.

    Never loosens a stop and never sets one on a losing trade: the stop can only ever end
    up at or better than break-even, so this cannot turn a live trade into a forced loss.
    """
    if not (BE_ENABLED and BE_TRIGGER_CASH > 0) or not hasattr(conn, "modify_position"):
        return 0
    spread = max(0.0, float(ask) - float(bid))
    moved = []
    for p in positions:
        try:
            profit = float(p.get("unrealizedProfit", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if profit < BE_TRIGGER_CASH:
            continue
        entry = float(p.get("openPrice", 0) or 0)
        vol = float(p.get("volume", 0) or 0)
        if not entry or not vol:
            continue
        is_buy = p.get("type") == "POSITION_TYPE_BUY"
        pid = p.get("id")
        peak = note_peak(pid, profit)
        # Lock break-even at the least, and otherwise whatever the peak leaves after the
        # trail - so a trade that ran to +1.80 keeps most of it instead of closing at zero.
        locked = max(BE_LOCK_CASH, peak - BE_TRAIL_CASH) if BE_TRAIL_CASH > 0 else BE_LOCK_CASH
        # The floor arms only once the peak is clear of it by the trail, or locking it the
        # instant profit touched $2 would close the trade there - the very cap we removed.
        if BE_FLOOR_CASH > 0 and peak >= BE_FLOOR_CASH + BE_TRAIL_CASH:
            locked = max(locked, BE_FLOOR_CASH)
        lock = tp_distance(vol, locked) or 0.0
        # A buy is opened at ask and closed at bid, so entry alone would close at -spread.
        target = round(entry + spread + lock, 2) if is_buy else round(entry - spread - lock, 2)
        # Keep it a placeable distance from the market rather than refusing outright.
        # The broker's real requirement when we have it; otherwise the old conservative
        # spread, because guessing low here means the modify is simply rejected.
        gap = max(gap_floor, BE_MIN_GAP) if gap_floor is not None else max(spread, BE_MIN_GAP)
        target = min(target, round(bid - gap, 2)) if is_buy else max(target, round(ask + gap, 2))
        # ...but never at the cost of turning a protected trade into a losing one.
        floor_px = (round(entry + spread, 2) if is_buy else round(entry - spread, 2))
        if (is_buy and target < floor_px) or (not is_buy and target > floor_px):
            # No stop can be placed above break-even yet. Say so once per trade instead
            # of skipping in silence - the silence is what made this look like the
            # protection "arriving late" with no reason given.
            if notify and not _be_waiting.get(pid):
                _be_waiting[pid] = True
                need = round(abs((floor_px + (gap if is_buy else -gap)) - entry)
                             * vol * GRID_CONTRACT, 2)
                await notify(
                    f"⏳ *صفقة ربحت `{profit:g}$` بس ما أكدر أحمّيها بعد*\n"
                    f"• أي ستوب الآن ينزل تحت نقطة التعادل — يعني ستوب خاسر\n"
                    f"• تنحمي عند `{need:g}$` (السبريد `{spread:.2f}$` + مسافة الوسيط "
                    f"`{gap:.2f}$`)\n"
                    f"• لين هذاك الوقت أحرسها بنفسي وأغلقها على التعادل لو رجعت")
            continue                      # not enough room yet to lock anything; wait
        current = p.get("stopLoss")
        try:
            current = float(current) if current else None
        except (TypeError, ValueError):
            current = None
        if current is not None and ((is_buy and current >= target) or (not is_buy and current <= target)):
            continue                      # already at or better than break-even
        try:
            await asyncio.wait_for(conn.modify_position(p["id"], stop_loss=target), timeout=8.0)
            moved.append((p["id"], target, current, locked, peak))
        except Exception as e:
            log.warning(f"break-even move failed on {p.get('id')}: {e}")
    live = {p.get("id") for p in positions}
    prune_peaks(live)
    for gone in [k for k in _be_waiting if k not in live]:
        _be_waiting.pop(gone, None)
    for pid, *_ in moved:
        _be_waiting.pop(pid, None)        # it is covered now; the warning can fire again
    if moved and notify:
        tail = (f"\nوالوقف يتبع الربح على بُعد `{BE_TRAIL_CASH:g}$` من القمة."
                if BE_TRAIL_CASH > 0 else "")
        # The message used to say only HOW MANY trades were secured. The owner looks
        # at MT5, where the one thing he can check is the price of the line on his
        # chart - and that was the one number we never gave him. Now every move
        # prints where the stop went, where it was, and what it guarantees, so a
        # claim of protection can be checked against his own screen in one glance.
        rows = []
        for pid, tgt, was, locked, pk in moved:
            frm = f" (كان `{was:g}`)" if was is not None else " (ما كان بيه وقف)"
            rows.append(f"• الوقف الآن على `{tgt:g}`{frm} — يضمن `{locked:g}$` "
                        f"· القمة `{pk:.2f}$`")
        await notify(f"🛡 *أمّنت `{len(moved)}` صفقة{(' ' + label) if label else ''}* — ربحت أكثر من "
                     f"`{BE_TRIGGER_CASH:g}$`، فما تقدر ترجع خسارة بعد الآن.{tail}\n"
                     + "\n".join(rows)
                     + "\n_افتح الـMT5: لازم تشوف خط الوقف على نفس هذا السعر._")
    return len(moved)
