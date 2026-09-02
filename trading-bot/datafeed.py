"""
datafeed.py - free market data, so MetaApi is only needed to place orders.

MetaApi bills per hour the account is deployed. Everything the bot does apart from
sending an order (charts, trend, levels, news context, the daily idea) only needs
prices, and prices are available for free. This module supplies them:

  1. Yahoo Finance chart API   - spot XAUUSD, futures GC=F, EURUSD, S&P 500. No key.
  2. Binance PAXG/USDT         - gold-backed token, trades 24/7 (covers the weekend).
  3. Stooq CSV                 - daily bars, last resort.

Calibration: a broker's GOLD quote is not identical to any public feed (spread,
futures basis, broker markup). Whenever MetaApi *is* awake the bot reports its real
price here; the median offset is stored and applied to free candles afterwards, so
support/resistance levels stay in the broker's own price frame while it sleeps.

This module never places orders and needs no API key.
"""

import asyncio
import json
import logging
import os
import statistics
import time

import httpx

log = logging.getLogger("harvester.datafeed")

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datafeed_state.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
MAX_SANE_OFFSET = 120.0   # a bigger gap than this means we compared the wrong instruments
CACHE_SECS = 60

# symbol -> yahoo ticker. Spot first: it is what a broker's GOLD actually tracks.
YAHOO = {
    "GOLD": "XAUUSD=X", "XAUUSD": "XAUUSD=X", "GOLD.": "XAUUSD=X",
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "US500": "^GSPC", "SPX500": "^GSPC", "US30": "^DJI", "NAS100": "^IXIC",
    "USDX": "DX-Y.NYB", "DXY": "DX-Y.NYB", "SILVER": "XAGUSD=X", "XAGUSD": "XAGUSD=X",
}
YAHOO_BACKUP = {"GOLD": "GC=F", "XAUUSD": "GC=F"}          # futures, if spot is empty
GOLD_SYMBOLS = {"GOLD", "XAUUSD", "GOLD.", "XAUUSD.", "GOLDmicro"}

# our timeframe -> (yahoo interval, yahoo range, how many yahoo bars make one of ours)
YF_TF = {
    "1m": ("1m", "1d", 1), "5m": ("5m", "5d", 1), "15m": ("15m", "1mo", 1),
    "30m": ("30m", "1mo", 1), "1h": ("1h", "3mo", 1),
    "4h": ("1h", "6mo", 4),      # Yahoo has no 4h bar: fold four 1h bars into one
    "1d": ("1d", "2y", 1),
}
BINANCE_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}

_cache = {}


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"samples": [], "offset": 0.0, "source": ""}


def _save_state(state):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"datafeed state save failed: {e}")


def note_broker_price(symbol, broker_price, free_price):
    """Called while MetaApi is awake: learn how far the free feed sits from the broker."""
    if not (broker_price and free_price):
        return
    diff = float(broker_price) - float(free_price)
    if abs(diff) > MAX_SANE_OFFSET:
        log.warning(f"ignoring implausible calibration diff {diff:.2f}")
        return
    st = _load_state()
    st["samples"] = (st.get("samples", []) + [round(diff, 3)])[-40:]
    st["offset"] = round(statistics.median(st["samples"]), 3)
    st["updated"] = int(time.time())
    _save_state(st)


def offset():
    st = _load_state()
    return float(st.get("offset", 0.0)) if st.get("samples") else 0.0


def calibration_line():
    st = _load_state()
    n = len(st.get("samples", []))
    if not n:
        return "📡 مصدر مجاني (غير معاير بعد — سيُعاير تلقائياً أول ما يشتغل الحساب)"
    return f"📡 مصدر مجاني معاير على سعر وسيطك (فرق `{st.get('offset', 0.0)}$` من {n} قياس)"


def _fold(candles, factor):
    """Fold N smaller bars into one bigger bar (open of first, high/low of all, close of last)."""
    if factor <= 1:
        return candles
    out = []
    for i in range(len(candles) - len(candles) % factor - factor, -1, -factor):
        group = candles[i:i + factor]
        out.append({
            "time": group[0]["time"],
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
            "tickVolume": sum(c.get("tickVolume", 0) for c in group),
        })
    return list(reversed(out))


async def _yahoo(client, ticker, timeframe, limit):
    interval, rng, factor = YF_TF.get(timeframe, ("1h", "3mo", 1))
    r = await client.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"interval": interval, "range": rng, "includePrePost": "false"},
    )
    r.raise_for_status()
    result = (r.json().get("chart") or {}).get("result") or []
    if not result:
        return []
    res = result[0]
    stamps = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    candles = []
    for i, ts in enumerate(stamps):
        o, h, l, c = q.get("open", [])[i], q.get("high", [])[i], q.get("low", [])[i], q.get("close", [])[i]
        if None in (o, h, l, c):
            continue
        candles.append({"time": ts, "open": o, "high": h, "low": l, "close": c,
                        "tickVolume": (q.get("volume") or [0] * len(stamps))[i] or 0})
    return _fold(candles, factor)[-limit:]


async def _binance_paxg(client, timeframe, limit):
    """PAXG is 1 token = 1 troy ounce of gold, and crypto never closes."""
    r = await client.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": "PAXGUSDT", "interval": BINANCE_TF.get(timeframe, "1h"),
                                 "limit": min(limit, 1000)})
    r.raise_for_status()
    return [{"time": int(k[0] / 1000), "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "tickVolume": float(k[5])} for k in r.json()]


async def _stooq_daily(client, code, limit):
    r = await client.get(f"https://stooq.com/q/d/l/", params={"s": code, "i": "d"})
    r.raise_for_status()
    rows = [ln.split(",") for ln in r.text.strip().splitlines()[1:]]
    out = []
    for row in rows[-limit:]:
        try:
            out.append({"time": row[0], "open": float(row[1]), "high": float(row[2]),
                        "low": float(row[3]), "close": float(row[4]), "tickVolume": 0})
        except (ValueError, IndexError):
            continue
    return out


async def get_candles(symbol, timeframe="1h", limit=200, apply_offset=True):
    """Free candles in the same shape MetaApi returns. Raises only if every source fails."""
    key = (symbol.upper(), timeframe, limit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECS:
        return hit[1]

    sym = symbol.upper()
    is_gold = sym in GOLD_SYMBOLS or sym.startswith("XAU")
    errors = []
    async with httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True) as client:
        attempts = []
        if sym in YAHOO:
            attempts.append(("yahoo", lambda: _yahoo(client, YAHOO[sym], timeframe, limit)))
        if sym in YAHOO_BACKUP:
            attempts.append(("yahoo-futures", lambda: _yahoo(client, YAHOO_BACKUP[sym], timeframe, limit)))
        if is_gold:
            attempts.append(("binance-paxg", lambda: _binance_paxg(client, timeframe, limit)))
            attempts.append(("stooq", lambda: _stooq_daily(client, "xauusd", limit)))
        if not attempts:  # unknown symbol: try it on Yahoo as-is
            attempts.append(("yahoo-raw", lambda: _yahoo(client, sym, timeframe, limit)))

        for name, fn in attempts:
            try:
                candles = await fn()
                if candles and len(candles) >= 30:
                    if is_gold and apply_offset:
                        off = offset()
                        if off:
                            candles = [{**c, "open": c["open"] + off, "high": c["high"] + off,
                                        "low": c["low"] + off, "close": c["close"] + off} for c in candles]
                    log.info(f"free candles {sym} {timeframe} via {name}: {len(candles)} bars")
                    _cache[key] = (time.time(), candles)
                    return candles
                errors.append(f"{name}: only {len(candles) if candles else 0} bars")
            except Exception as e:
                errors.append(f"{name}: {e}")
    raise RuntimeError("free data unavailable -> " + " | ".join(errors[:4]))


async def get_price(symbol="GOLD"):
    """Approximate bid/ask from the last free close (used while MetaApi sleeps)."""
    candles = await get_candles(symbol, "15m", 60)
    last = candles[-1]["close"]
    return {"bid": round(last - 0.15, 2), "ask": round(last + 0.15, 2), "approx": True}


if __name__ == "__main__":  # manual check on a machine with open internet
    async def main():
        for sym, tf in (("GOLD", "15m"), ("GOLD", "4h"), ("EURUSD", "1h"), ("US500", "1h")):
            try:
                c = await get_candles(sym, tf, 120)
                print(f"{sym:8} {tf:4} bars={len(c):4} last={c[-1]['close']}")
            except Exception as e:
                print(f"{sym:8} {tf:4} FAILED: {e}")
        print(await get_price("GOLD"))
        print(calibration_line())
    asyncio.run(main())
