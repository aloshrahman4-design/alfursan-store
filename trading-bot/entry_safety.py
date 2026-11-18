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

import hashlib
import json
import os
import time

__all__ = [
    "MANUAL_MAGIC", "is_ours", "owner_label", "may_close", "may_protect",
    "would_duplicate", "duplicate_report", "needs_real_stop", "reconcile",
    "feed_is_stale", "entry_allowed",
    "EntryBlocked", "require_entry_permission", "entry_lock_state",
    "MAGIC_TEST", "TEST_LOT_CAP", "TEST_SL_DOLLARS", "TEST_TTL_SECS",
    "grant_test_permit", "revoke_test_permit", "test_permit",
    "consume_test_permit", "mark_permit_unknown", "mark_permit_done",
    "permit_line", "test_mode_active",
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


def require_entry_permission(*, stop_loss=None, lot=None, market=False, conn=None,
                            permit=None):
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
    if permit is not None:
        # The single-trade execution test. A token exists only because
        # consume_test_permit() already wrote "consumed" to the disk and fsynced
        # it, so by the time we are here the permit is spent whatever happens next.
        _check_permit(permit, stop_loss, lot)
        return
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


# --------------------------------------------------------------------------- 6
# THE ONE-TRADE EXECUTION TEST
#
# The history says this strategy loses: 2308 closed trades, 75.8% of them winners,
# and still -2537.80, because the average loss is 4.18x the average win. So this
# mode is NOT here to find out whether the edge is real. It is here to answer three
# mechanical questions on a live account for about three and a half dollars: does a
# stop actually reach the broker, does MT5 draw it, and does it follow the price.
#
# Everything below is built so that ONE trade is the most it can ever produce. The
# permit is a file on disk, it is spent before the order is sent rather than after,
# a send whose outcome is unknown spends it just the same, and nothing anywhere
# hands it back. If that costs a wasted permit the owner re-issues it in one line;
# the opposite mistake costs an unbounded loop of live orders.

MAGIC_TEST = 777555                  # its own magic: the grid must never count it
TEST_LOT_CAP = 0.01
TEST_SL_DOLLARS = 3.0                # price distance, and on 0.01 lot of gold
TEST_TTL_SECS = 3600                 # a permit nobody used is not a standing licence
PERMIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "test_permit.json")

# granted  -> issued, nothing sent
# consumed -> spent, the order is about to go or has gone
# unknown  -> the send timed out; whether it reached the broker is NOT known
# done     -> a position was confirmed, or the trade is finished
# revoked  -> withdrawn by the owner
_LIVE_STATES = ("granted",)
_SPENT_STATES = ("consumed", "unknown", "done")


def _read_permit():
    try:
        with open(PERMIT_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _write_permit(d):
    """Write it and make the kernel admit it is on the disk before returning.

    Without the flush and fsync the record can sit in a buffer while the order is
    already at the broker. A crash in that window loses the only evidence the
    permit was spent, and the next start issues the same trade again.
    """
    tmp = PERMIT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PERMIT_PATH)
    try:
        fd = os.open(os.path.dirname(PERMIT_PATH), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def test_permit(now=None):
    """The permit as it stands, with expiry already applied. None when there is none."""
    d = _read_permit()
    if not d:
        return None
    now = time.time() if now is None else now
    if d.get("state") in _LIVE_STATES and now - float(d.get("granted_at", 0)) > TEST_TTL_SECS:
        d = dict(d, state="expired")
        try:
            _write_permit(d)
        except Exception:
            pass
    return d


def test_mode_active(now=None):
    """True while a permit exists in any state other than finished or withdrawn.

    The grid stays refused for the whole of that window, not just while the permit
    is unspent: a ladder armed next to a live test trade is exactly the extra
    exposure this mode promises not to create.
    """
    d = test_permit(now)
    return bool(d and d.get("state") in (_LIVE_STATES + ("consumed", "unknown")))


def grant_test_permit(side, lot=TEST_LOT_CAP, sl_dollars=TEST_SL_DOLLARS, now=None):
    """Issue the single permit. (ok, message). Refuses to overwrite a spent one."""
    side = str(side or "").strip().lower()
    if side not in ("buy", "sell"):
        return False, "الاتجاه لازم يكون `buy` أو `sell`"
    try:
        lot = float(lot)
        sl_dollars = float(sl_dollars)
    except (TypeError, ValueError):
        return False, "لوت أو وقف غير صالح"
    if not (0 < lot <= TEST_LOT_CAP):
        return False, "اللوت لازم يكون `%g` أو أقل" % TEST_LOT_CAP
    if sl_dollars <= 0:
        return False, "الوقف لازم يكون موجباً"
    old = test_permit(now)
    if old and old.get("state") in _SPENT_STATES:
        return False, ("الإذن السابق انصرف (`%s`). ما أعطي إذناً ثانياً لحالي — "
                       "`/testtrade off` يمسحه إذا متأكد إنه ماكو صفقة مفتوحة."
                       % old.get("state"))
    _write_permit({"side": side, "lot": lot, "sl_dollars": sl_dollars,
                   "granted_at": float(time.time() if now is None else now),
                   "state": "granted", "token": None, "note": ""})
    return True, "تم"


def revoke_test_permit():
    """Withdraw it. Always possible, including after it was spent."""
    d = _read_permit()
    if not d:
        return False
    try:
        _write_permit(dict(d, state="revoked"))
    except Exception:
        return False
    return True


def consume_test_permit(side, lot, now=None):
    """Spend the permit BEFORE the order is sent. (token, error).

    The write happens here, with fsync, and the caller may only send once this has
    returned a token. Spending after a successful send would be simpler and wrong:
    the gap between the two is where a crash turns one permitted trade into a loop.
    """
    d = test_permit(now)
    if not d:
        return None, "ماكو إذن — `/testtrade buy` أو `/testtrade sell`"
    st = d.get("state")
    if st == "expired":
        return None, "الإذن انتهت مدته (`%d` دقيقة)" % (TEST_TTL_SECS // 60)
    if st == "revoked":
        return None, "الإذن مسحوب"
    if st in _SPENT_STATES:
        return None, "الإذن انصرف من قبل (`%s`) — ما تنفتح صفقة ثانية" % st
    if str(side).lower() != d.get("side"):
        return None, "الإذن على `%s` مو `%s`" % (d.get("side"), side)
    try:
        if float(lot) > float(d.get("lot", TEST_LOT_CAP)) + 1e-9:
            return None, "اللوت أكبر من الإذن"
    except (TypeError, ValueError):
        return None, "لوت غير صالح"
    token = hashlib.sha256(
        ("%s|%s|%s" % (d.get("granted_at"), side, os.urandom(16).hex())).encode()
    ).hexdigest()[:32]
    _write_permit(dict(d, state="consumed", token=token,
                       consumed_at=float(time.time() if now is None else now)))
    return token, ""


def mark_permit_unknown(note=""):
    """The send timed out. Whether it reached the broker is not known - say so.

    There is deliberately no retry anywhere near this. A timeout is not a failure;
    it is an absence of information, and the one thing that must not follow is a
    second order sent on the assumption that the first did not arrive.
    """
    d = _read_permit()
    if d:
        _write_permit(dict(d, state="unknown", note=str(note)[:200]))


def mark_permit_done(position_id=None, note=""):
    d = _read_permit()
    if d:
        _write_permit(dict(d, state="done", position_id=str(position_id or ""),
                           note=str(note)[:200]))


def _check_permit(token, stop_loss, lot):
    """The live-order gate for this mode. Raises unless everything lines up."""
    d = _read_permit()
    if not d or d.get("state") != "consumed":
        raise EntryBlocked("no consumed test permit")
    if not token or token != d.get("token"):
        raise EntryBlocked("test permit token does not match")
    try:
        if float(lot) > TEST_LOT_CAP + 1e-9 or float(lot) <= 0:
            raise EntryBlocked("test trade is capped at %g lot" % TEST_LOT_CAP)
        sl = float(stop_loss)
    except (TypeError, ValueError, OverflowError):
        raise EntryBlocked("test trade requires a numeric broker-side stop loss")
    if not (sl > 0 and sl < float("inf")):
        raise EntryBlocked("test trade requires a positive broker-side stop loss")


def permit_line(now=None):
    """One readable line about the permit, for the panel. Never raises."""
    d = test_permit(now)
    if not d:
        return "🔒 وضع اختبار التنفيذ: ماكو إذن"
    st = d.get("state")
    words = {"granted": "🟡 إذن فعّال — صفقة وحدة",
             "consumed": "🔵 انصرف — الأمر أُرسل",
             "unknown": "🚨 مجهول — أُرسل وما نعرف وصل",
             "done": "✅ انتهى",
             "revoked": "⛔️ مسحوب",
             "expired": "⌛️ انتهت مدته"}
    left = ""
    if st == "granted":
        secs = TEST_TTL_SECS - ((time.time() if now is None else now)
                                - float(d.get("granted_at", 0)))
        left = " · يبقى `%d` دقيقة" % max(0, int(secs // 60))
    return ("%s · `%s` `%g` لوت · وقف `%g$`%s"
            % (words.get(st, st), d.get("side", "?"), d.get("lot", 0),
               d.get("sl_dollars", 0), left))
