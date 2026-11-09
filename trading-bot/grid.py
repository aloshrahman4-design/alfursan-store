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
GRID_TP_CASH = _f("GRID_TP_CASH", 5.0)        # close the whole basket at this profit
GRID_MAX_LOSS = _f("GRID_MAX_LOSS", 0.0)      # 0 = work it out from the percentage
GRID_MAX_LOSS_PCT = _f("GRID_MAX_LOSS_PCT", 15.0)
GRID_MARGIN_CEILING_PCT = _f("GRID_MARGIN_CEILING_PCT", 40.0)


def ladder(base=None, cap=None, levels=None, double_every=None):
    """The lot for each level: doubling, but never past the cap."""
    base = GRID_BASE_LOT if base is None else base
    cap = GRID_MAX_LOT if cap is None else cap
    levels = GRID_LEVELS if levels is None else levels
    double_every = GRID_DOUBLE_EVERY if double_every is None else max(1, double_every)
    return [round(min(base * 2 ** (i // double_every), cap), 2) for i in range(max(0, levels))]


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


def loss_limit(balance=0.0):
    """The floating loss at which the basket is closed and the grid stands down."""
    if GRID_MAX_LOSS > 0:
        return GRID_MAX_LOSS
    return round(max(1.0, float(balance) * GRID_MAX_LOSS_PCT / 100.0), 2)


def _blank():
    return {"armed": False, "center": None, "armed_at": 0, "cycles": 0,
            "realized": 0.0, "halted": False, "halt_reason": ""}


def load():
    try:
        with open(STATE_PATH) as f:
            state = _blank()
            state.update(json.load(f))
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
    save(_blank())


async def grid_positions(conn):
    pos = await asyncio.wait_for(conn.get_positions(), timeout=6.0)
    return [p for p in pos if p.get("magic") in MAGICS]


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
    worst, best = worst_case(lots), best_case(lots)
    limit = loss_limit(balance)
    shown = ", ".join(f"{x:g}" for x in lots[:10]) + (" …" if len(lots) > 10 else "")
    lines = [
        f"• المستويات: `{len(lots)}` لكل جهة · المسافة `{GRID_STEP:g}$`",
        f"• السلّم: `{shown}`",
        f"• أكبر لوت: `{max(lots) if lots else 0:g}` · المجموع `{total:g}` لوت",
        f"• مدى الشبكة: `{round(len(lots) * GRID_STEP, 2):g}$` بكل اتجاه",
        f"• جني الباسكت: `+{GRID_TP_CASH:g}$` · وقف الشبكة: `-{limit:g}$`",
        f"• لو امتلأت كلها ورجع السعر: `-{worst:,.0f}$`",
        f"• لو امتلأت واستمر بنفس الاتجاه: `+{best:,.0f}$`",
    ]
    if balance:
        lines.append(f"• أسوأ حالة = `{round(worst / balance * 100)}%` من رصيدك `{balance:g}$`")
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
        for maker, price_at, magic in (
                (conn.create_stop_buy_order, up, MAGIC_GRID_BUY),
                (conn.create_stop_sell_order, down, MAGIC_GRID_SELL)):
            try:
                await asyncio.wait_for(
                    maker(symbol=symbol, volume=lot, open_price=price_at,
                          options={"magic": magic}), timeout=8.0)
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


async def grid_engine(get_conn, symbol, notify, is_active):
    """Watch the basket: take it on profit, cut it on loss, re-centre when it empties."""
    warned_unsupported = False
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
            orders = await grid_orders(conn)
            floating = round(sum(float(p.get("unrealizedProfit", 0.0) or 0.0) for p in positions), 2)

            acct = await asyncio.wait_for(conn.get_account_information(), timeout=6.0)
            limit = loss_limit(float(acct.get("balance", 0) or 0))

            if positions and floating >= GRID_TP_CASH:
                closed, booked, cancelled = await teardown(conn)
                state["cycles"] = state.get("cycles", 0) + 1
                state["realized"] = round(state.get("realized", 0.0) + booked, 2)
                state["armed"] = False
                save(state)
                await notify(f"💰 *حصدت الشبكة* `+{booked:g}$`\n"
                             f"• أغلقت `{closed}` صفقة وألغيت `{cancelled}` أمر معلّق\n"
                             f"• الدورة رقم `{state['cycles']}` · الإجمالي `{state['realized']:g}$`\n"
                             "🕸 أنصبها من جديد على السعر الحالي...")
                continue

            if positions and floating <= -limit:
                closed, booked, cancelled = await teardown(conn)
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

            if not positions and not orders:
                state["armed"] = False
                save(state)
                continue

            await asyncio.sleep(1.5)
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
    if lots and max(lots) >= GRID_MAX_LOT:
        body.append(f"• 🔒 السقف `{GRID_MAX_LOT:g}` فعّال — بدونه أكبر لوت كان "
                    f"`{round(GRID_BASE_LOT * 2 ** ((len(lots) - 1) // GRID_DOUBLE_EVERY), 2):g}`")
    body.append("\n`/grid on` · `/grid off` · `/grid reset` · `/setkey gridlevels 12`")
    return "\n".join(body)
