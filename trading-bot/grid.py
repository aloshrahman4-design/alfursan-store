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
BE_SOFT = os.environ.get("BE_SOFT", "1") not in ("0", "false", "no")
BE_SOFT_EXIT = _f("BE_SOFT_EXIT", 0.05)
# Hard ceilings on what the grid may be holding at once. The level cap only ever limited
# PENDING orders per side; nothing limited open positions, so refilling and recycling kept
# feeding a trend until twenty were open together and the basket stop took them all at once.
GRID_MAX_POSITIONS = _i("GRID_MAX_POSITIONS", 8)
# And a soft ceiling in money: stop adding once the basket is most of the way to its stop.
GRID_ADD_STOP_PCT = _f("GRID_ADD_STOP_PCT", 50.0)
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
            "loss_streak": 0, "pending_ids": []}


def load():
    try:
        with open(STATE_PATH) as f:
            state = _blank()
            state.update(json.load(f))
            state.setdefault("released", [])
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
    fresh = _blank()
    old = load()
    fresh["released"] = old.get("released", [])
    # The losing run survives a re-centre on purpose: recovery sizing is about the run of
    # cycles, and zeroing it here would make every re-arm look like a fresh start.
    fresh["loss_streak"] = old.get("loss_streak", 0)
    save(fresh)
    trend_reset()


async def grid_positions(conn, exclude=None):
    """Grid trades the bot still owns. Released ones are the user's and stay invisible."""
    exclude = set(exclude if exclude is not None else load().get("released", []))
    pos = await asyncio.wait_for(conn.get_positions(), timeout=6.0)
    note_read_ok()
    return [p for p in pos if p.get("magic") in MAGICS and p.get("id") not in exclude]


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


async def ensure_take_profits(conn, positions, notify=None):
    """Put the per-trade target on any grid position that is missing one.

    Attaching it only when the order is placed leaves every position opened before the
    setting existed - or by an order placed under an older build - running with no exit of
    its own. The user watched a level sit at +$2.43 and give it all back for exactly that
    reason, so the target is reconciled on what is open, not only on what is sent.
    """
    if GRID_TP_PER_TRADE <= 0:
        return 0
    fixed = []
    for p in positions:
        if p.get("takeProfit"):
            continue
        dist = tp_distance(p.get("volume", 0))
        if not dist:
            continue
        entry = float(p.get("openPrice", 0) or 0)
        if not entry:
            continue
        is_buy = p.get("type") == "POSITION_TYPE_BUY"
        tp = round(entry + dist, 2) if is_buy else round(entry - dist, 2)
        try:
            await asyncio.wait_for(conn.modify_position(p["id"], take_profit=tp), timeout=8.0)
            fixed.append((p["id"], tp))
        except Exception as e:
            log.warning(f"could not set take profit on {p.get('id')}: {e}")
    if fixed and notify:
        await notify(f"🎯 *حطيت هدف `+{GRID_TP_PER_TRADE:g}$` على `{len(fixed)}` صفقة شبكة "
                     "كانت بلا هدف* — صارت تغلق لحالها عند الوسيط.")
    return len(fixed)


async def grid_orders(conn):
    if not hasattr(conn, "get_orders"):
        return []
    try:
        orders = await asyncio.wait_for(conn.get_orders(), timeout=6.0)
    except Exception as e:
        log.warning(f"could not read pending orders: {e}")
        return []
    return [o for o in orders if o.get("magic") in MAGICS]


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


def plan_text(balance=0.0):
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
    lines = [
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
        (f"• ✂️ التنصيف: كل `{HALF_DISTANCE:g}$` يبتعد السعر عن صفقة خاسرة، "
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


async def arm(conn, symbol, notify):
    """Place the ladder on both sides of the current price."""
    price = await asyncio.wait_for(conn.get_symbol_price(symbol=symbol), timeout=6.0)
    bid, ask = float(price.get("bid", 0) or 0), float(price.get("ask", 0) or 0)
    if not bid or not ask:
        return False, "ماكو سعر"
    center = round((bid + ask) / 2.0, 2)
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
    need = sum(lots) * bias_sides() * center * GRID_CONTRACT / max(1.0, leverage)
    ceiling = balance * GRID_MARGIN_CEILING_PCT / 100.0
    if need > min(free, ceiling):
        return False, (f"الهامش المطلوب `{need:,.0f}$` أكبر من المسموح "
                       f"`{min(free, ceiling):,.0f}$` — قلّل المستويات أو اللوت")

    placed, failed = 0, 0
    for i, lot in enumerate(lots, start=1):
        up = round(center + i * GRID_STEP, 2)
        down = round(center - i * GRID_STEP, 2)
        dist = tp_distance(lot)
        for maker, price_at, magic, sign in (
                (conn.create_stop_buy_order, up, MAGIC_GRID_BUY, 1),
                (conn.create_stop_sell_order, down, MAGIC_GRID_SELL, -1)):
            if not bias_allows(magic):
                continue              # the user has stood this direction down by hand
            tp = round(price_at + sign * dist, 2) if dist else None
            try:
                res = await asyncio.wait_for(
                    maker(symbol=symbol, volume=lot, open_price=price_at,
                          take_profit=tp, options={"magic": magic}), timeout=8.0)
                remember_order((res or {}).get("orderId"))
                placed += 1
            except Exception as e:
                failed += 1
                log.warning(f"grid level {i} at {price_at} failed: {e}")
    if not placed:
        return False, "ما انحطت ولا أمر — راجع اسم الأداة وحجم اللوت عند وسيطك"

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
    await notify(f"🕸 *نصبت الشبكة حول* `{center}`\n\n{plan_text(balance)}\n"
                 f"• أوامر معلّقة: `{placed}`{note}{rec}")
    return True, None


_balance_cache = {"at": 0.0, "value": 0.0}


async def cached_balance(conn):
    now = time.time()
    if now - _balance_cache["at"] < GRID_BALANCE_TTL and _balance_cache["value"]:
        return _balance_cache["value"]
    acct = await asyncio.wait_for(conn.get_account_information(), timeout=6.0)
    _balance_cache.update({"at": now, "value": float(acct.get("balance", 0) or 0)})
    return _balance_cache["value"]


# How often the engine repeats "I cannot lay a ladder yet". Often enough to be seen,
# rarely enough not to become noise on a grid that sits at its cap for an hour.
BLOCKED_NOTE_SECS = _f("BLOCKED_NOTE_SECS", 900.0)
_blocked_at = 0.0


async def grid_engine(get_conn, symbol, notify, is_active):
    """Watch the basket: take it on profit, cut it on loss, re-centre when it empties."""
    warned_unsupported = False
    # best each side has shown; in memory, so a peak dies with the basket that earned it
    peaks = {"buy": 0.0, "sell": 0.0, "all": 0.0}
    while True:
        try:
            if not (GRID_ENABLED and is_active()):
                await asyncio.sleep(3)
                continue
            conn = get_conn()
            if conn is None:
                await asyncio.sleep(3)
                continue
            global _last_conn
            _last_conn = conn
            # Blind for too long? Pull the orders before they open anything else.
            if not reads_free(conn) and blind_for() > BLIND_SECS and not _blind["cut"]:
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
                await asyncio.sleep(5)
                continue
            if not state.get("armed"):
                held = await grid_positions(conn)
                if len(held) >= GRID_MAX_POSITIONS:
                    # It used to sleep here in total silence, which is how a changed lot
                    # looked like a setting that "did not work": the value was saved, the
                    # ladder that would carry it simply could never be laid. Say it.
                    global _blocked_at
                    if time.time() - _blocked_at > BLOCKED_NOTE_SECS:
                        _blocked_at = time.time()
                        lots = ladder(base=recovery_base(state.get("loss_streak", 0)))
                        await notify(
                            f"⏳ *ما أقدر أنصب سلّم جديد* — عندي `{len(held)}` صفقة مفتوحة "
                            f"وهذا سقف الصفقات (`{GRID_MAX_POSITIONS}`).\n"
                            f"• أي إعداد غيّرته للوت محفوظ وينطبق على السلّم الجاي: "
                            f"`{', '.join(f'{x:g}' for x in lots[:6])} …`\n"
                            "• أنصبه تلقائياً أول ما تنفك صفقة وحدة\n"
                            "• تريده هسه؟ سكّر صفقة بالمنصة، أو `/grid off` ثم `/grid on`")
                    await asyncio.sleep(5)   # too crowded to lay a new ladder
                    continue
                ok, err = await arm(conn, symbol, notify)
                if not ok:
                    await notify(f"⚠️ ما كدرت أنصب الشبكة: {err}")
                    state["halted"] = True
                    state["halt_reason"] = err or "?"
                    save(state)
                await asyncio.sleep(2)
                continue

            positions = await grid_positions(conn)
            if hasattr(conn, "modify_position"):
                await ensure_take_profits(conn, positions, notify)
            orders = await grid_orders(conn)
            buys = [p for p in positions if p.get("magic") == MAGIC_GRID_BUY]
            sells = [p for p in positions if p.get("magic") == MAGIC_GRID_SELL]
            floating = pnl(positions)

            limit = loss_limit(await cached_balance(conn))

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
                quote = await asyncio.wait_for(
                    conn.get_symbol_price(symbol=symbol), timeout=6.0)
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
            if near_stop:
                room = 0                  # already most of the way to the stop
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
                    why = ("الخسارة قربت من الوقف" if near_stop
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


def status_text(balance=0.0):
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
    body = [head, "", plan_text(balance), "",
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
    if not (GRID_REFILL and GRID_LEVELS > 0):
        return 0
    if room is not None and room <= 0:
        return 0
    try:
        if quote is None:
            quote = await asyncio.wait_for(conn.get_symbol_price(symbol=symbol), timeout=6.0)
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
            try:
                await asyncio.wait_for(conn.cancel_order(furthest["id"]), timeout=6.0)
                recycled += 1
            except Exception as e:
                log.warning(f"could not recycle {furthest.get('id')}: {e}")
                continue
        try:
            res = await asyncio.wait_for(
                maker(symbol=symbol, volume=lot, open_price=at,
                      take_profit=round(at + sign * dist, 2) if dist else None,
                      options={"magic": magic}), timeout=8.0)
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
        if dist < HALF_DISTANCE * (done + 1):
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


# The broker's own minimum distance between a stop and the market, in PRICE. Asked for
# once and kept: it is a contract term, not a quote. None means "never got an answer",
# and the caller then keeps the old conservative behaviour rather than guessing.
_stops = {"value": None, "asked": 0.0}
STOPS_RETRY_SECS = _f("STOPS_RETRY_SECS", 600.0)


def stops_reset():
    _stops.update({"value": None, "asked": 0.0})


async def broker_stop_gap(conn, symbol):
    """Smallest gap the broker allows between a stop and the market, in price units.

    `stopsLevel` is quoted in POINTS, so it is scaled by the symbol's digits: on gold with
    2 digits, 20 points is 0.20$. Returns None when the broker has not told us, and the
    answer is cached so this costs one request per session, not one per tick.
    """
    if _stops["value"] is not None:
        return _stops["value"]
    if time.time() - _stops["asked"] < STOPS_RETRY_SECS:
        return None
    _stops["asked"] = time.time()
    if not hasattr(conn, "get_symbol_specification"):
        return None
    try:
        spec = await asyncio.wait_for(
            conn.get_symbol_specification(symbol), timeout=6.0)
    except Exception as e:
        log.warning(f"could not read the broker stop distance: {e}")
        return None
    try:
        points = float((spec or {}).get("stopsLevel", 0) or 0)
        digits = int((spec or {}).get("digits", 2) or 2)
    except (TypeError, ValueError):
        return None
    gap = round(points * (10 ** -digits), 5)
    _stops["value"] = gap
    log.info(f"broker stop distance for {symbol}: {points} points = {gap}$")
    return gap


_be_peaks = {}        # best profit each secured position has shown; dies with the position
_be_waiting = {}      # id -> True once the user has been told this one cannot be stopped yet


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
        peak = _be_peaks.get(pid, 0.0)
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
        _be_peaks.pop(pid, None)
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
        peak = max(_be_peaks.get(pid, 0.0), profit)
        _be_peaks[pid] = peak
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
            moved.append((p["id"], target))
        except Exception as e:
            log.warning(f"break-even move failed on {p.get('id')}: {e}")
    live = {p.get("id") for p in positions}
    for gone in [k for k in _be_peaks if k not in live]:
        _be_peaks.pop(gone, None)
    for gone in [k for k in _be_waiting if k not in live]:
        _be_waiting.pop(gone, None)
    for pid, _ in moved:
        _be_waiting.pop(pid, None)        # it is covered now; the warning can fire again
    if moved and notify:
        tail = (f"\nوالوقف يتبع الربح على بُعد `{BE_TRAIL_CASH:g}$` من القمة."
                if BE_TRAIL_CASH > 0 else "")
        await notify(f"🛡 *أمّنت `{len(moved)}` صفقة{(' ' + label) if label else ''}* — ربحت أكثر من "
                     f"`{BE_TRIGGER_CASH:g}$`، فما تقدر ترجع خسارة بعد الآن.{tail}")
    return len(moved)
