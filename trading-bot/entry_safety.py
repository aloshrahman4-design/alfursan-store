"""The rules that decide what the bot is ALLOWED to touch, and what it must refuse.

Every rule here was written because its absence cost real money or real hours on
this account. The module holds no state of its own and talks to no broker: it takes
plain dictionaries - the shapes MetaApi returns - and answers questions. That makes
each rule testable on its own, which the rules living inside the engine were not.

Five concerns, in the order they bite:

1. OWNERSHIP. The single authority on "is this ours?". A position opened by hand in
   MT5 carries magic 0 and is never the bot's, no matter how profitable, unless the
   owner adopted it by id. Six rounds of this project were spent guessing from chart
   screenshots why a winning trade had no stop; the answer was that nothing in the
   bot considered it its own, and nothing said so.

2. PERMISSION TO CLOSE. Tightening a stop is safe in every state. Closing is not.
   They were one decision in the engine, so a pause that had to stop closing also
   stopped protecting.

3. DUPLICATES. A grid re-arm that cannot see what is already placed stacks a second
   ladder on top of the first, doubling the exposure with no message.

4. RECONCILIATION. What the bot believes is placed versus what the broker actually
   holds. Four separate defects in this project were "the bot could not see it, so
   it acted as though it were not there".

5. A BROKEN FEED. When MetaApi stops answering, a stale picture is indistinguishable
   from a quiet market. Opening into that is how a hedge becomes a naked position.
"""

import time

__all__ = [
    "MANUAL_MAGIC", "is_ours", "owner_label", "may_close", "may_protect",
    "would_duplicate", "duplicate_report", "needs_real_stop", "reconcile",
    "feed_is_stale", "entry_allowed",
]

# A position MetaTrader opened for a human rather than an EA. Never ours by default.
MANUAL_MAGIC = 0


# --------------------------------------------------------------------------- 1
def is_ours(position, magics, adopted=(), released=()):
    """The one place that decides whether the bot manages a position.

    `released` wins over everything: a trade handed to the owner is never taken
    back quietly. `adopted` is the owner asking, by id, for a trade the bot did not
    open. Everything else is ours only if its magic is one of ours - so magic 0,
    the hand-opened trade, is excluded by construction rather than by remembering
    to check for it at each call site.
    """
    pid = str((position or {}).get("id"))
    if pid in {str(i) for i in (released or ())}:
        return False
    if pid in {str(i) for i in (adopted or ())}:
        return True
    return (position or {}).get("magic") in tuple(magics or ())


def owner_label(position, magics, adopted=(), released=()):
    """Why a position is or is not ours, in one short phrase for the user."""
    pid = str((position or {}).get("id"))
    if pid in {str(i) for i in (released or ())}:
        return "released"
    if pid in {str(i) for i in (adopted or ())}:
        return "adopted"
    mg = (position or {}).get("magic")
    if mg in tuple(magics or ()):
        return "bot"
    return "manual" if mg in (MANUAL_MAGIC, None) else "foreign-ea"


# --------------------------------------------------------------------------- 2
def may_close(position, magics, adopted=(), released=(), allowed=True):
    """Closing is an action. It needs ownership AND the system switch to be on."""
    return bool(allowed) and is_ours(position, magics, adopted, released)


def may_protect(position, magics, adopted=(), released=()):
    """Tightening a stop in the owner's favour is safe whatever the system switch.

    Deliberately NOT gated on `allowed`: /pause must stop the bot from trading, not
    from guarding what is already open. Conflating the two left live trades
    unprotected every time the owner paused the system.
    """
    return is_ours(position, magics, adopted, released)


# --------------------------------------------------------------------------- 3
def would_duplicate(price, is_buy, existing, tolerance):
    """True when something of the same side already sits within `tolerance` of price.

    `existing` is any mix of orders and positions; both carry openPrice and a type
    string containing BUY or SELL. The tolerance should be a fraction of the grid
    step - two rungs closer together than that are the same rung twice.
    """
    try:
        price = float(price)
        tolerance = abs(float(tolerance))
    except (TypeError, ValueError):
        return False
    for e in existing or ():
        kind = str((e or {}).get("type", "")).upper()
        if not kind:
            continue
        other_buy = "BUY" in kind
        if other_buy != bool(is_buy):
            continue
        try:
            at = float(e.get("openPrice", e.get("price", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if at and abs(at - price) <= tolerance:
            return True
    return False


def duplicate_report(existing, tolerance):
    """Groups of same-side entries sitting on top of each other, worst first.

    Finds a ladder that was armed twice. Returns a list of
    (side, price, count) for each cluster holding more than one entry.
    """
    try:
        tolerance = abs(float(tolerance))
    except (TypeError, ValueError):
        return []
    buckets = {}
    for e in existing or ():
        kind = str((e or {}).get("type", "")).upper()
        if not kind:
            continue
        side = "buy" if "BUY" in kind else "sell"
        try:
            at = float(e.get("openPrice", e.get("price", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if not at:
            continue
        key = (side, round(at / tolerance) if tolerance else at)
        buckets.setdefault(key, []).append(at)
    out = [(side, round(sum(v) / len(v), 2), len(v))
           for (side, _k), v in buckets.items() if len(v) > 1]
    return sorted(out, key=lambda r: -r[2])


# --------------------------------------------------------------------------- 4
def needs_real_stop(position, trigger_cash, magics, adopted=(), released=()):
    """A position of ours, past the trigger, carrying no broker stop.

    A MetaApi trailing rule is not a broker stop: MT5 draws nothing for it and it
    dies with the MetaApi connection. Only a stopLoss price is held by the broker,
    and only a stopLoss price is visible to the owner on his own screen.
    """
    if not is_ours(position, magics, adopted, released):
        return False
    try:
        profit = float((position or {}).get("unrealizedProfit", 0) or 0)
    except (TypeError, ValueError):
        return False
    if profit < float(trigger_cash or 0):
        return False
    sl = (position or {}).get("stopLoss")
    try:
        return not (sl and float(sl))
    except (TypeError, ValueError):
        return True


def reconcile(expected_order_ids, broker_orders, broker_positions,
              magics, trigger_cash=0.0, adopted=(), released=(), tolerance=0.0):
    """Compare what the bot believes with what the broker actually holds.

    Returns a dict, every key a list, every list empty when things agree:
      vanished    - ids the bot is still counting that the broker does not have
      unexpected  - OUR orders at the broker that the bot is not tracking
      unprotected - our positions past the trigger with no broker stop
      duplicates  - same-side entries stacked within `tolerance`
      foreign     - positions on this account the bot will never manage
    """
    have_ids = {str(o.get("id")) for o in (broker_orders or [])}
    want_ids = {str(i) for i in (expected_order_ids or ())}
    ours_orders = [o for o in (broker_orders or [])
                   if o.get("magic") in tuple(magics or ())]
    return {
        "vanished": sorted(want_ids - have_ids),
        "unexpected": sorted(str(o.get("id")) for o in ours_orders
                             if str(o.get("id")) not in want_ids),
        "unprotected": [p for p in (broker_positions or [])
                        if needs_real_stop(p, trigger_cash, magics, adopted, released)],
        "duplicates": duplicate_report(list(broker_orders or []) +
                                       list(broker_positions or []), tolerance)
        if tolerance else [],
        "foreign": [p for p in (broker_positions or [])
                    if owner_label(p, magics, adopted, released)
                    in ("manual", "foreign-ea")],
    }


# --------------------------------------------------------------------------- 5
def feed_is_stale(last_ok_ts, max_age_secs, now=None):
    """True when the last successful broker read is older than we can trade on."""
    try:
        last = float(last_ok_ts or 0)
        max_age = float(max_age_secs or 0)
    except (TypeError, ValueError):
        return True
    if max_age <= 0:
        return False
    if last <= 0:
        return True                      # never read anything: assume the worst
    return (now if now is not None else time.time()) - last > max_age


def entry_allowed(last_ok_ts, max_age_secs, connected=True, now=None):
    """(ok, reason). Whether it is safe to OPEN anything right now.

    Protection is never gated on this - a stale feed is a reason to stop adding
    risk, not a reason to stop guarding what is already open.
    """
    if not connected:
        return False, "ماكو اتصال بالوسيط"
    if feed_is_stale(last_ok_ts, max_age_secs, now):
        return False, "آخر قراءة ناجحة قديمة — الصورة اللي عندي ما تنعتمد"
    return True, ""
