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
GRID_BASE_LOT = _f("GRID_BASE_LOT", 0.01)
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
GRID_TP_PER_TRADE = _f("GRID_TP_PER_TRADE", 2.0)
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
BE_TRIGGER_CASH = _f("BE_TRIGGER_CASH", 1.0)
BE_LOCK_CASH = _f("BE_LOCK_CASH", 0.10)
# Break-even alone saves the trade and loses the gain: a position that reached +$1.80 and
# turned around closed at zero, with the 1.80 gone. Once secured, the stop keeps following
# the peak this far behind it, so a reversal banks most of what the move actually gave.
BE_TRAIL_CASH = _f("BE_TRAIL_CASH", 0.50)
# Both sides armed at once means a move that should earn is paid for by the other side
# bleeding at the same rate: price climbs $3, the buys gain it, the open sells give it
# straight back, and the basket nets pennies. In chop the grid grinds itself down that way.
# So once a side commits, the other side's pending orders are pulled and the basket becomes
# directional; both sides are armed again at the next re-arm.
GRID_ONE_WAY = os.environ.get("GRID_ONE_WAY", "1") not in ("0", "false", "no")
# One filled level is not a trend, it is a wiggle: price touching the first step above the
# centre once was enough to cancel the whole opposite side and leave a reversal uncaught.
# A direction has to prove itself over this many levels before the other side is pulled.
GRID_COMMIT_LEVELS = max(1, _i("GRID_COMMIT_LEVELS", 2))
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


def loss_limit(balance=0.0):
    """The floating loss at which the basket is closed and the grid stands down."""
    if GRID_MAX_LOSS > 0:
        return GRID_MAX_LOSS
    return round(max(1.0, float(balance) * GRID_MAX_LOSS_PCT / 100.0), 2)


def _blank():
    return {"armed": False, "center": None, "armed_at": 0, "cycles": 0,
            "realized": 0.0, "halted": False, "halt_reason": "", "released": []}


def load():
    try:
        with open(STATE_PATH) as f:
            state = _blank()
            state.update(json.load(f))
            state.setdefault("released", [])
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
    fresh["released"] = load().get("released", [])
    save(fresh)


async def grid_positions(conn, exclude=None):
    """Grid trades the bot still owns. Released ones are the user's and stay invisible."""
    exclude = set(exclude if exclude is not None else load().get("released", []))
    pos = await asyncio.wait_for(conn.get_positions(), timeout=6.0)
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


async def cancel_all(conn):
    cancelled = 0
    for o in await grid_orders(conn):
        try:
            await asyncio.wait_for(conn.cancel_order(o["id"]), timeout=6.0)
            cancelled += 1
        except Exception as e:
            log.warning(f"could not cancel {o.get('id')}: {e}")
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
    lots = ladder()
    total = round(sum(lots), 2)
    margin_each = GRID_CONTRACT  # unknown price here; the caller fills real margin in
    worst, best, whip = worst_case(lots), best_case(lots), whipsaw_case(lots)
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
        (f"• 🧭 اتجاه واحد: بعد `{GRID_COMMIT_LEVELS}` مستويات بجهة وحدة، تنلغي أوامر الجهة الثانية"
         if GRID_ONE_WAY else "• ⚠️ الجهتين مفتوحات سوا — الربح ممكن تاكله الجهة المعاكسة"),
        ("• ✂️ يحصد كل جهة لحالها" if GRID_SIDE_HARVEST else "• يحصد المجموع الكلي"),
        (f"• 🛡 التأمين: أول ما تربح `{BE_TRIGGER_CASH:g}$` ينقل وقفها للدخول، "
         f"وبعدها يتبع الربح على بُعد `{BE_TRAIL_CASH:g}$` من القمة"
         if BE_ENABLED else "• ⚠️ ماكو تأمين على نقطة الدخول"),
        (f"• 🧩 يسدّ الفجوات: أمر جديد إذا ابتعد أقرب مستوى أكثر من `{GRID_GAP_FACTOR:g}` خطوة"
         if GRID_REFILL else "• ⚠️ ما يسدّ الفجوات — ممكن يبقى بلا أوامر قرب السعر"),
        ("• 🤝 الخاسرة تبقى مفتوحة وتصير إلك — البوت ما يتحكم بيها"
         if GRID_RELEASE_LOSERS else "• الخاسرة تنغلق مع الرابحة"),
        f"• لو امتلأت جهة ورجع السعر للوسط: `-{worst:,.0f}$`",
        f"• لو تأرجح وكسر الجهتين: `-{whip:,.0f}$` ← وهنا يقطع الوقف عند `-{limit:g}$`",
        f"• لو امتلأت واستمر بنفس الاتجاه: `+{best:,.0f}$`",
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
    lots = ladder()
    if not lots:
        return False, "ماكو مستويات"

    acct = await asyncio.wait_for(conn.get_account_information(), timeout=6.0)
    balance = float(acct.get("balance", 0) or 0)
    free = float(acct.get("freeMargin", acct.get("equity", balance)) or balance)
    leverage = float(acct.get("leverage", 500) or 500)
    need = sum(lots) * 2 * center * GRID_CONTRACT / max(1.0, leverage)
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
            tp = round(price_at + sign * dist, 2) if dist else None
            try:
                await asyncio.wait_for(
                    maker(symbol=symbol, volume=lot, open_price=price_at,
                          take_profit=tp, options={"magic": magic}), timeout=8.0)
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
    await notify(f"🕸 *نصبت الشبكة حول* `{center}`\n\n{plan_text(balance)}\n"
                 f"• أوامر معلّقة: `{placed}`{note}")
    return True, None


_balance_cache = {"at": 0.0, "value": 0.0}


async def cached_balance(conn):
    now = time.time()
    if now - _balance_cache["at"] < GRID_BALANCE_TTL and _balance_cache["value"]:
        return _balance_cache["value"]
    acct = await asyncio.wait_for(conn.get_account_information(), timeout=6.0)
    _balance_cache.update({"at": now, "value": float(acct.get("balance", 0) or 0)})
    return _balance_cache["value"]


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
                              "realized": round(state.get("realized", 0.0) + booked, 2)})
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

            if GRID_ONE_WAY and positions and orders:
                live_sides = {p.get("magic") for p in positions}
                if len(live_sides) == 1 and len(positions) >= GRID_COMMIT_LEVELS:
                    committed = next(iter(live_sides))
                    stale = [o for o in orders if o.get("magic") != committed]
                    for o in stale:
                        try:
                            await asyncio.wait_for(conn.cancel_order(o["id"]), timeout=6.0)
                        except Exception as e:
                            log.warning(f"could not cancel opposite order {o.get('id')}: {e}")
                    if stale:
                        way = "الشراء" if committed == MAGIC_GRID_BUY else "البيع"
                        await notify(f"🧭 *السعر التزم جهة {way}* (`{len(positions)}` مستويات) — "
                                     f"ألغيت `{len(stale)}` أمر معلّق بالجهة المعاكسة "
                                     "حتى ما تاكل الربح.")

            if positions and BE_ENABLED:
                try:
                    px = await asyncio.wait_for(conn.get_symbol_price(symbol=symbol), timeout=6.0)
                    sp = max(0.0, float(px.get("ask", 0) or 0) - float(px.get("bid", 0) or 0))
                    await apply_breakeven(conn, positions, sp, notify)
                except Exception as e:
                    log.warning(f"break-even pass skipped: {e}")

            if orders or positions:
                await refill_gaps(conn, symbol, positions, orders, notify)

            if not positions and not orders:
                peaks = {"buy": 0.0, "sell": 0.0, "all": 0.0}
                state["armed"] = False
                save(state)
                continue

            # A trailing exit is only as tight as how often it looks. While a basket is in
            # the money the give-back threshold is live, so watch it three times as closely;
            # the rest of the time there is nothing to miss.
            await asyncio.sleep(0.5 if floating >= GRID_TP_CASH else 1.5)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"grid engine error: {e}", exc_info=True)
            await asyncio.sleep(5)


def status_text(balance=0.0):
    state = load()
    lots = ladder()
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
    if lots and max(lots) >= GRID_MAX_LOT:
        body.append(f"• 🔒 السقف `{GRID_MAX_LOT:g}` فعّال — بدونه أكبر لوت كان "
                    f"`{round(GRID_BASE_LOT * 2 ** ((len(lots) - 1) // GRID_DOUBLE_EVERY), 2):g}`")
    body.append("\n`/grid on` · `/grid off` · `/grid reset` · `/setkey gridlevels 12`")
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


async def refill_gaps(conn, symbol, positions, orders, notify=None):
    """Put a level back within one step of price when the nearest one has drifted away.

    Never touches an open position and never exceeds GRID_LEVELS pendings a side, so a
    trending market tops up at most to the size the ladder was always allowed to be.
    """
    if not (GRID_REFILL and GRID_LEVELS > 0):
        return 0
    try:
        price = await asyncio.wait_for(conn.get_symbol_price(symbol=symbol), timeout=6.0)
    except Exception as e:
        log.warning(f"refill skipped, no price: {e}")
        return 0
    bid, ask = float(price.get("bid", 0) or 0), float(price.get("ask", 0) or 0)
    if not bid or not ask:
        return 0
    lot = round(min(GRID_BASE_LOT, GRID_MAX_LOT), 2)
    dist = tp_distance(lot)
    gap = GRID_STEP * GRID_GAP_FACTOR
    added, recycled = 0, 0

    for magic, maker, ref, sign in (
            (MAGIC_GRID_BUY, conn.create_stop_buy_order, ask, 1),
            (MAGIC_GRID_SELL, conn.create_stop_sell_order, bid, -1)):
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
            await asyncio.wait_for(
                maker(symbol=symbol, volume=lot, open_price=at,
                      take_profit=round(at + sign * dist, 2) if dist else None,
                      options={"magic": magic}), timeout=8.0)
            added += 1
            log.info(f"refilled {'buy' if sign > 0 else 'sell'} level at {at}")
        except Exception as e:
            log.warning(f"refill at {at} failed: {e}")
    if added and notify:
        extra = (f"\nنقلت `{recycled}` أمر من أبعد نقطة بدل ما أزيد العدد." if recycled else "")
        await notify(f"🧩 *سدّيت فجوة بالشبكة* — أضفت `{added}` أمر قرب السعر الحالي، "
                     f"الصفقات المفتوحة ما لمستها.{extra}")
    return added


_be_peaks = {}        # best profit each secured position has shown; dies with the position


async def apply_breakeven(conn, positions, spread, notify=None, label="الشبكة"):
    """Move the stop of any position past the trigger to its entry, costs covered.

    Never loosens a stop and never sets one on a losing trade: the stop can only ever end
    up at or better than break-even, so this cannot turn a live trade into a forced loss.
    """
    if not (BE_ENABLED and BE_TRIGGER_CASH > 0) or not hasattr(conn, "modify_position"):
        return 0
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
        lock = tp_distance(vol, locked) or 0.0
        # A buy is opened at ask and closed at bid, so entry alone would close at -spread.
        target = round(entry + spread + lock, 2) if is_buy else round(entry - spread - lock, 2)
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
    if moved and notify:
        tail = (f"\nوالوقف يتبع الربح على بُعد `{BE_TRAIL_CASH:g}$` من القمة."
                if BE_TRAIL_CASH > 0 else "")
        await notify(f"🛡 *أمّنت `{len(moved)}` صفقة {label}* — ربحت أكثر من "
                     f"`{BE_TRIGGER_CASH:g}$`، فما تقدر ترجع خسارة بعد الآن.{tail}")
    return len(moved)
