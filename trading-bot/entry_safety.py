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

import os
import time

__all__ = [
    "MANUAL_MAGIC", "is_ours", "owner_label", "may_close", "may_protect",
    "would_duplicate", "duplicate_report", "needs_real_stop", "reconcile",
    "feed_is_stale", "entry_allowed",
    "EntryBlocked", "require_entry_permission", "entry_lock_state",
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


# --------------------------------------------------------------------------- 0
# THE INTERLOCK. Added on the range-harvester-safety-test branch before the rules
# above existed, and kept here with its behaviour unchanged: it refuses EVERY new
# order, unconditionally, whatever the opt-in says. That last refusal is not a bug
# and must not be "tidied away" - it is the lock the owner asked to stay shut until
# the tests pass and he says otherwise. The checks above it still run first so the
# reason the owner sees names the real problem (no stop, lot too big) rather than
# the blanket refusal, and entry_lock_state() lets the bot SAY it is locked instead
# of looking broken.

ALLOW_ENV = "RH_ALLOW_NEW_ORDERS"
ALLOW_PHRASE = "YES_I_ACCEPT_RISK"
INTERIM_LOT_CAP = 0.01


class EntryBlocked(RuntimeError):
    """Raised instead of opening anything. Never raised by a close or a stop move."""


def _is_simulator(conn):
    """True only for the in-process paper broker, which cannot reach a real account.

    Decided from the object's own class chain, not from a setting, so no
    environment variable can make a live connection look like a simulator.
    """
    if conn is None:
        return False
    try:
        chain = type(conn).__mro__
    except Exception:
        return False
    if any(getattr(c, "__module__", "") == "broker_paper" for c in chain):
        return True
    # A test double declares itself in CODE, with an attribute on its own class.
    # Deliberately not a setting: an environment variable or a config line could be
    # flipped on the live server by accident or by a careless paste, and this must
    # not be flippable from outside the source of the object itself.
    return getattr(conn, "__simulator__", False) is True


def require_entry_permission(*, stop_loss=None, lot=None, market=False, conn=None):
    """Explicit opt-in plus mandatory broker-side stop for any new exposure.

    This is an interim safety interlock, NOT a complete live risk gate.

    The paper broker is exempt, and only the paper broker. Without that the engine
    could not be exercised at all, which would have meant shipping a trading bot
    whose trading had never been run - and "we could not test it" is not a safer
    state than "we tested it in a simulator". Everything that can reach real money
    still ends at the unconditional refusal below.
    """
    if _is_simulator(conn):
        return                      # a simulator: no account, no money, no broker
    if os.environ.get(ALLOW_ENV, "").strip() != ALLOW_PHRASE:
        raise EntryBlocked("new orders disabled: %s is not explicitly enabled" % ALLOW_ENV)
    try:
        volume = float(lot)
        sl = float(stop_loss)
    except (TypeError, ValueError, OverflowError):
        raise EntryBlocked("new orders require a valid lot and broker-side stop loss")
    if not (0 < volume <= INTERIM_LOT_CAP):
        raise EntryBlocked("lot exceeds interim %g cap or is invalid" % INTERIM_LOT_CAP)
    if not (sl > 0 and sl < float("inf")):
        raise EntryBlocked("new orders require a positive broker-side stop loss")
    if market:
        raise EntryBlocked("market entries blocked until stop-loss and reconciliation "
                           "are integrated")
    # No live broker snapshot/reconciliation is wired into this interlock yet.
    # Deliberately refuse ALL new entries even when the legacy opt-in is set.
    raise EntryBlocked("new orders locked until live risk gate and broker "
                       "reconciliation are integrated")


def entry_lock_state(conn=None):
    """(locked, one line the owner can read). Never raises, so a status call is safe.

    Without this the only way to learn the lock was shut was to watch every order
    fail and read a broker-shaped error that the broker never sent.
    """
    if _is_simulator(conn):
        return False, "\U0001f9ea \u0645\u062d\u0627\u0643\u064a \u0648\u0631\u0642\u064a \u2014 \u0645\u0627\u0643\u0648 \u062d\u0633\u0627\u0628 \u0648\u0644\u0627 \u0641\u0644\u0648\u0633"
    opted = os.environ.get(ALLOW_ENV, "").strip() == ALLOW_PHRASE
    if not opted:
        return True, ("\U0001f512 \u0642\u0641\u0644 \u0627\u0644\u0623\u0645\u0627\u0646 \u0645\u063a\u0644\u0642: \u0645\u0627\u0643\u0648 \u0623\u0648\u0627\u0645\u0631 \u062c\u062f\u064a\u062f\u0629 \u0646\u0647\u0627\u0626\u064a\u0627\u064b "
                      "(`%s` \u0645\u0648 \u0645\u0641\u0639\u0651\u0644)" % ALLOW_ENV)
    return True, ("\U0001f512 \u0642\u0641\u0644 \u0627\u0644\u0623\u0645\u0627\u0646 \u0645\u063a\u0644\u0642: \u0627\u0644\u0645\u0648\u0627\u0641\u0642\u0629 \u0645\u062d\u0641\u0648\u0638\u0629\u060c \u0628\u0633 \u0627\u0644\u0642\u0641\u0644 \u0644\u0627 \u064a\u0632\u0627\u0644 "
                  "\u064a\u0631\u0641\u0636 \u0643\u0644 \u0623\u0645\u0631 \u062c\u062f\u064a\u062f \u0628\u0642\u0635\u062f \u2014 \u062d\u062a\u0649 \u062a\u0646\u0631\u0628\u0637 \u0628\u0648\u0627\u0628\u0629 \u0627\u0644\u0645\u062e\u0627\u0637\u0631 \u0627\u0644\u062d\u064a\u0629")
