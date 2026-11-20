"""Replay the hour-range breakout over real candles and report what it did.

Written because the honest answer about this strategy was "unknown". The grid at
least had 2308 closed trades to convict it; this one has nothing, and shipping a
second strategy on a feeling after the first lost 2537$ would be the same mistake
with better code.

It calls the LIVE decision functions - range_break.plan for the levels and
grid.giveback_floor for the exit - rather than reimplementing them. A backtest
that reimplements the rules measures the reimplementation, and this project has
already been burned three times by two copies of one rule disagreeing.

What it cannot model, stated plainly so the number is not over-read:
  - Fills are assumed at the trigger price. Real fills slip, and a stop order
    fills WORSE than its trigger by definition, so every result here is optimistic.
  - Within one candle it cannot know whether the high or the low came first. When
    a candle touches both the entry and the stop, it is counted as a loss. That is
    the pessimistic choice and it is deliberate: the alternative flatters.
  - Swap, commission and weekend gaps are not modelled beyond the spread.
A result that is barely positive here should be read as negative.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import grid                                    # noqa: E402  the live exit rule
import range_break as rb                       # noqa: E402  the live level rule


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def run(candles, spread=0.32, lot=0.01, contract=100.0, verbose=False):
    """Walk the candles hour by hour. Returns a dict of what happened.

    One position at a time, exactly as the engine allows. Each hour: read the
    closed candle, place the two traps, then walk forward candle by candle until
    one springs, the level goes stale, or the data runs out.
    """
    per = lot * contract                       # dollars per 1.00 of price
    cost = spread * per                        # one round trip
    trades, equity, peak_eq, worst_dd = [], 0.0, 0.0, 0.0
    # `i < n`, not `i < n - 1`: the old bound never let the final candle be a
    # trigger bar, which silently dropped the last hour of every run and made a
    # two-candle fixture produce nothing at all.
    i, n = 1, len(candles)
    expiry_bars = max(1, int(rb.BREAK_EXPIRY_MINS / 60)) if rb.BREAK_EXPIRY_MINS else 10 ** 9

    while i < n:
        prev = candles[i - 1]
        p, _why = rb.plan(_f(prev.get("high")), _f(prev.get("low")))
        if p is None:
            i += 1
            continue

        entry = side = None
        for j in range(i, min(i + expiry_bars, n)):
            hi, lo = _f(candles[j].get("high")), _f(candles[j].get("low"))
            up = hi >= p["buy_at"]
            dn = lo <= p["sell_at"]
            if up and dn:
                # Both edges touched inside one candle. Which came first is not in
                # the data, so take the loser's view: filled, then stopped.
                trades.append({"side": "both", "pnl": -(p["stop"] * per + cost),
                               "peak": 0.0, "bar": j, "reason": "whipsaw"})
                equity += trades[-1]["pnl"]
                i = j + 1
                break
            if up or dn:
                side = "buy" if up else "sell"
                entry = p["buy_at"] if up else p["sell_at"]
                stop = p["buy_sl"] if up else p["sell_sl"]
                i = j
                break
        else:
            i += 1
            continue
        if entry is None:
            continue

        # walk the open trade
        peak_cash, pnl, closed_at = 0.0, None, None
        for k in range(i, n):
            hi, lo = _f(candles[k].get("high")), _f(candles[k].get("low"))
            if side == "buy":
                if lo <= stop:
                    pnl, closed_at = -(abs(entry - stop) * per + cost), "stop"
                    break
                best = (hi - entry) * per
            else:
                if hi >= stop:
                    pnl, closed_at = -(abs(stop - entry) * per + cost), "stop"
                    break
                best = (entry - lo) * per
            peak_cash = max(peak_cash, best)
            floor = grid.giveback_floor(peak_cash)
            if peak_cash >= grid.GIVEBACK_ARM_CASH and floor is not None:
                # The give-back exit, measured in the same cash terms the live
                # guard uses. Taken at the floor, minus the round trip.
                close = _f(candles[k].get("close"))
                now = (close - entry) * per if side == "buy" else (entry - close) * per
                if now <= floor:
                    pnl, closed_at = floor - cost, "giveback"
                    break
        if pnl is None:
            close = _f(candles[-1].get("close"))
            now = (close - entry) * per if side == "buy" else (entry - close) * per
            pnl, closed_at = now - cost, "open-at-end"
        trades.append({"side": side, "pnl": round(pnl, 2), "peak": round(peak_cash, 2),
                       "bar": k, "reason": closed_at})
        equity += pnl
        peak_eq = max(peak_eq, equity)
        worst_dd = min(worst_dd, equity - peak_eq)
        i = k + 1

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    aw = (gp / len(wins)) if wins else 0.0
    al = (gl / len(losses)) if losses else 0.0
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / len(trades), 1) if trades else 0.0,
        "need_rate": round(100.0 * al / (aw + al), 1) if (aw + al) else 0.0,
        "avg_win": round(aw, 2), "avg_loss": round(al, 2),
        "profit_factor": round(gp / gl, 2) if gl else (float("inf") if gp else 0.0),
        "net": round(equity, 2), "worst_drawdown": round(worst_dd, 2),
        "spread_paid": round(len(trades) * cost, 2),
        "whipsaws": sum(1 for t in trades if t["reason"] == "whipsaw"),
        "by_reason": {r: sum(1 for t in trades if t["reason"] == r)
                      for r in ("stop", "giveback", "whipsaw", "open-at-end")},
        "detail": trades if verbose else [],
    }


def report(r, label=""):
    """The same shape /perf prints, so the two can be compared directly."""
    verdict = ("🟢 موجب" if r["profit_factor"] > 1.1 else
               "🟡 حدّي" if r["profit_factor"] >= 0.95 else "🔴 خاسر")
    return "\n".join([
        f"📊 *قياس تاريخي{(' — ' + label) if label else ''}*",
        f"• صفقات `{r['trades']}` · رابحة `{r['wins']}` · خاسرة `{r['losses']}`",
        f"• نسبة النجاح `{r['win_rate']}%` · المطلوبة للتعادل `{r['need_rate']}%`",
        f"• متوسط الربح `{r['avg_win']}$` · متوسط الخسارة `{r['avg_loss']}$`",
        f"• عامل الربح `{r['profit_factor']}`  {verdict}",
        f"• الصافي `{r['net']:+}$` · أسوأ هبوط `{r['worst_drawdown']}$`",
        f"• دفعناه سبريد `{r['spread_paid']}$` · تذبذبات ضربتنا `{r['whipsaws']}`",
        "_الأرقام متفائلة: التنفيذ بلا انزلاق، والشمعة المزدوجة تُحسب خسارة._",
    ])


async def from_feed(symbol, timeframe="1h", limit=1000, **kw):
    """Fetch candles the same way the bot does, then replay them."""
    import datafeed
    candles = await datafeed.get_candles(symbol, timeframe, limit)
    return run(candles, **kw)


if __name__ == "__main__":
    import asyncio
    sym = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SYMBOL", "XAUUSD")
    tf = sys.argv[2] if len(sys.argv) > 2 else "1h"
    print(report(asyncio.run(from_feed(sym, tf)), f"{sym} {tf}"))
