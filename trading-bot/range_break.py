"""Two traps at the edges of the last hour, and nothing in between.

The owner's own description, in his words: don't guess where price is going - set
one trap above and one below, and walk with whichever springs.

Every strategy this project has run until now entered BEFORE knowing the
direction and paid for the privilege. The grid did it 2308 times and handed the
broker about 739$ of a 2537$ loss in spread alone. His straddle idea did it twice
per attempt, which costs 23.20$ every time gold wobbles 2$ each way - and
wobbling is what gold mostly does. The scalping idea could not be executed at all
on a cloud bridge with a second and a half of latency.

One change answers all three: the orders are PENDING, parked at the broker.
  - The broker fills them at the level, so the bot's latency stops mattering.
  - A quiet hour fills nothing, and costs nothing. That is the whole saving.
  - Only one side can fill, because the other is cancelled the moment it does.

What this module does NOT do, on purpose:
  - It does not decide when to exit. grid.take_giveback owns that, and this
    project has already paid dearly for having two owners of one rule.
  - It does not place market orders, ever.
  - It does not touch a position it did not create, and never magic 0.
  - It does not bypass the entry interlock. On a live account every order here is
    refused until that lock is deliberately opened.
"""

import asyncio
import logging
import os
import time

import entry_safety as safety

log = logging.getLogger("harvester")

MAGIC_BREAK = 777666          # its own, so nothing else counts or closes these


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


BREAK_ENABLED = os.environ.get("BREAK_ENABLED", "0") in ("1", "true", "yes")
BREAK_LOT = _f("BREAK_LOT", 0.01)
BREAK_TF = os.environ.get("BREAK_TF", "1h")
# How far outside the range the traps sit. A fraction of the range itself, not a
# fixed number of dollars: the same 1.5$ that is sensibly clear of a quiet hour is
# inside the noise of a violent one, and a level inside the noise is a coin flip
# that charges a spread.
BREAK_EDGE_PCT = _f("BREAK_EDGE_PCT", 10.0)
# The stop, as a share of the range. Same reasoning.
BREAK_STOP_PCT = _f("BREAK_STOP_PCT", 60.0)
BREAK_MIN_RANGE = _f("BREAK_MIN_RANGE", 1.0)      # an hour flatter than this is not a range
BREAK_MAX_RANGE = _f("BREAK_MAX_RANGE", 30.0)     # nor is a news candle a range
BREAK_MAX_LOSSES = _i("BREAK_MAX_LOSSES", 3)      # losses in a day before standing down
BREAK_EXPIRY_MINS = _i("BREAK_EXPIRY_MINS", 120)  # a trap nobody sprang is a stale level


def settings_line():
    """Everything that governs this, in one line, read from what is actually used."""
    return (f"🎯 كسر النطاق: {'🟢 شغّال' if BREAK_ENABLED else '🔴 مطفي'} · "
            f"لوت `{BREAK_LOT:g}` · إطار `{BREAK_TF}` · "
            f"حافة `{BREAK_EDGE_PCT:g}%` · وقف `{BREAK_STOP_PCT:g}%` من النطاق · "
            f"حد الخسائر اليومي `{BREAK_MAX_LOSSES}`")


# --------------------------------------------------------------------------- levels
def last_closed_range(candles, now=None):
    """(high, low) of the most recent CLOSED candle, or None.

    The candle still forming is deliberately excluded. Its high and low are not
    facts yet - they move - and a trap placed on a number that is still changing
    is placed on nothing.
    """
    if not candles or len(candles) < 2:
        return None
    done = candles[:-1]                      # drop the one still being written
    c = done[-1]
    try:
        hi, lo = float(c["high"]), float(c["low"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (hi > lo > 0):
        return None
    return hi, lo


def plan(hi, lo):
    """Where the two traps and their stops go. None when the hour is unusable.

    Returns a dict, or (None, reason) phrased for the owner.
    """
    rng = round(hi - lo, 2)
    if rng < BREAK_MIN_RANGE:
        return None, f"النطاق `{rng}$` أضيق من `{BREAK_MIN_RANGE:g}$` — ساعة ميتة، ما أنصب"
    if rng > BREAK_MAX_RANGE:
        return None, f"النطاق `{rng}$` أوسع من `{BREAK_MAX_RANGE:g}$` — خبر أو انفجار، ما أنصب"
    edge = round(rng * BREAK_EDGE_PCT / 100.0, 2)
    stop = round(rng * BREAK_STOP_PCT / 100.0, 2)
    return {
        "range": rng,
        "buy_at": round(hi + edge, 2),
        "sell_at": round(lo - edge, 2),
        "buy_sl": round(hi + edge - stop, 2),
        "sell_sl": round(lo - edge + stop, 2),
        "stop": stop,
        "edge": edge,
    }, ""


def risk_cash(stop, lot=None, contract=100.0):
    """What one wrong trap costs, in dollars, before slippage."""
    return round(abs(float(stop)) * float(lot if lot is not None else BREAK_LOT)
                 * float(contract), 2)


# --------------------------------------------------------------------------- state
_state = {"orders": [], "placed_at": 0.0, "level": None, "losses": 0, "day": ""}


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def roll_day():
    """A new day clears the loss count. Returns True when it just rolled."""
    if _state["day"] != _today():
        _state.update(day=_today(), losses=0)
        return True
    return False


def note_loss():
    roll_day()
    _state["losses"] += 1
    return _state["losses"]


def stood_down():
    """True when today's losses have used up the allowance."""
    roll_day()
    return BREAK_MAX_LOSSES > 0 and _state["losses"] >= BREAK_MAX_LOSSES


def armed_ids():
    return list(_state["orders"])


def forget():
    _state.update(orders=[], placed_at=0.0, level=None)


# --------------------------------------------------------------------------- acting
async def cancel_all(conn, ids=None):
    """Pull the traps. Returns how many actually went."""
    gone = 0
    for oid in list(ids if ids is not None else _state["orders"]):
        try:
            await asyncio.wait_for(conn.cancel_order(oid), timeout=8.0)
            gone += 1
        except Exception as e:
            log.warning(f"could not cancel {oid}: {e}")
    if ids is None:
        forget()
    return gone


async def arm(conn, symbol, candles, notify=None, lot=None, contract=100.0):
    """Place the two traps for the hour just closed. Returns (ok, message).

    Nothing here opens a position. Both orders carry their stop at the broker from
    the instant they exist, so a fill is protected before the bot hears about it -
    which matters, because the bot hears about it a second and a half later.
    """
    if not BREAK_ENABLED:
        return False, "كسر النطاق مطفي — `/setkey break 1` يشغّله"
    if stood_down():
        return False, (f"وقفت لليوم: `{_state['losses']}` خسائر "
                       f"(الحد `{BREAK_MAX_LOSSES}`). يرجع بكرة لحاله.")
    got = last_closed_range(candles)
    if not got:
        return False, "ماكو شمعة مغلقة أقرا منها النطاق"
    hi, lo = got
    p, why = plan(hi, lo)
    if p is None:
        return False, why
    lot = float(lot if lot is not None else BREAK_LOT)

    placed, failed = [], []
    for maker, price, sl, side in (
            (getattr(conn, "create_stop_buy_order", None), p["buy_at"], p["buy_sl"], "شراء"),
            (getattr(conn, "create_stop_sell_order", None), p["sell_at"], p["sell_sl"], "بيع")):
        if maker is None:
            failed.append((side, "الوسيط ما يدعم الأوامر المعلّقة"))
            continue
        try:
            # Through the interlock like everything else that opens exposure. On a
            # live account this refuses, and that refusal is the lock working.
            safety.require_entry_permission(stop_loss=sl, lot=lot, conn=conn)
            res = await asyncio.wait_for(
                maker(symbol=symbol, volume=lot, open_price=price, stop_loss=sl,
                      options={"magic": MAGIC_BREAK}), timeout=10.0)
            oid = (res or {}).get("orderId")
            if oid:
                placed.append(oid)
        except Exception as e:
            failed.append((side, str(e)[:120]))

    if placed and failed:
        # One trap alone is a directional bet he never asked for. Pull it.
        await cancel_all(conn, placed)
        placed = []
    if not placed:
        detail = "\n".join(f"• {s}: `{w}`" for s, w in failed) or "• (ماكو سبب)"
        return False, f"ما انحطت الفخاخ:\n{detail}"

    _state.update(orders=placed, placed_at=time.time(), level=(hi, lo))
    if notify:
        await notify(
            f"🪤 *نصبت فخّين على نطاق آخر {BREAK_TF}*\n"
            f"• النطاق `{lo}` — `{hi}` (عرضه `{p['range']}$`)\n"
            f"• شراء إذا طلع فوق `{p['buy_at']}` · وقفه `{p['buy_sl']}`\n"
            f"• بيع إذا نزل تحت `{p['sell_at']}` · وقفه `{p['sell_sl']}`\n"
            f"• لو انطبق واحد، أمسح الثاني فوراً\n"
            f"• أقصى خسارة للمحاولة `{risk_cash(p['stop'], lot, contract)}$`\n"
            f"• وإذا ما انطبق ولا واحد: **ما ندفع ولا سنت**")
    return True, ""


async def watch(conn, notify=None):
    """One pass: did a trap spring, or go stale? Returns a short word for the log.

    The cancel is the whole point of the pair: the moment one fills, the other is
    a naked bet in the opposite direction on a market that has just chosen a side.
    """
    if not _state["orders"]:
        return "idle"
    try:
        live = {str(o.get("id")) for o in (await asyncio.wait_for(
            conn.get_orders(), timeout=8.0) or [])}
    except Exception as e:
        # Cannot see the book. Do NOT assume a fill and cancel the survivor - that
        # would strip a real trade of its pair on a network hiccup.
        log.warning(f"range-break watch could not read orders: {e}")
        return "blind"
    mine = [o for o in _state["orders"] if str(o) in live]
    if len(mine) == len(_state["orders"]):
        if (BREAK_EXPIRY_MINS > 0
                and time.time() - _state["placed_at"] > BREAK_EXPIRY_MINS * 60):
            n = await cancel_all(conn)
            if notify and n:
                await notify(f"🧹 *سحبت الفخاخ* — عدّى `{BREAK_EXPIRY_MINS}` دقيقة "
                             f"وما انطبق ولا واحد. المستوى صار قديم.")
            return "expired"
        return "waiting"
    if not mine:
        forget()
        return "both-gone"
    # One is missing: it filled. Pull the other.
    n = await cancel_all(conn, mine)
    forget()
    if notify:
        await notify("🎯 *انطبق فخ* — مسحت الثاني.\n"
                     "• الوقف عند الوسيط من لحظة الفتح\n"
                     "• ومن هنا الحارس يمسك القمة ويغلق على الارتداد")
    return "sprung"
