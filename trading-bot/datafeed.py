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
CACHE_SECS = int(os.environ.get("FEED_CACHE_SECS", "60"))
# One-minute bars cached for a minute means half the resolution is stale by
# the time it is read. Short timeframes get a short cache.
FAST_CACHE_SECS = int(os.environ.get("FEED_FAST_CACHE_SECS", "15"))

# symbol -> yahoo ticker. Spot first: it is what a broker's GOLD actually tracks.
YAHOO = {
    "GOLD": "XAUUSD=X", "XAUUSD": "XAUUSD=X", "GOLD.": "XAUUSD=X",
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "US500": "^GSPC", "SPX500": "^GSPC", "US30": "^DJI", "NAS100": "^IXIC",
    "USDX": "DX-Y.NYB", "DXY": "DX-Y.NYB", "SILVER": "XAGUSD=X", "XAGUSD": "XAGUSD=X",
}
YAHOO_BACKUP = {"GOLD": "GC=F", "XAUUSD": "GC=F"}          # futures, if spot is empty
GOLD_SYMBOLS = {"GOLD", "XAUUSD", "GOLD.", "XAUUSD.", "GOLDmicro"}

# Crypto -> Binance spot pair. Binance is the right primary here, not Yahoo: it is free,
# needs no key, serves true 1-minute klines and never closes - which is the whole point
# of the free-price path. Brokers spell the same instrument a dozen ways, so the lookup
# strips the decoration (BTCUSD., BTCUSD_raw, BTC/USD, #BTCUSD) before matching.
CRYPTO = {
    "BTCUSD": "BTCUSDT", "BTCUSDT": "BTCUSDT", "BTC": "BTCUSDT", "BITCOIN": "BTCUSDT",
    "BTCUSDC": "BTCUSDT", "XBTUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT", "ETHUSDT": "ETHUSDT", "ETH": "ETHUSDT", "ETHEREUM": "ETHUSDT",
    "LTCUSD": "LTCUSDT", "XRPUSD": "XRPUSDT", "SOLUSD": "SOLUSDT", "BNBUSD": "BNBUSDT",
    "ADAUSD": "ADAUSDT", "DOGEUSD": "DOGEUSDT",
}
CRYPTO_YAHOO = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD", "LTCUSDT": "LTC-USD",
                "XRPUSDT": "XRP-USD", "SOLUSDT": "SOL-USD", "BNBUSDT": "BNB-USD",
                "ADAUSDT": "ADA-USD", "DOGEUSDT": "DOGE-USD"}


def bare(symbol):
    """Strip broker decoration: 'BTCUSD.', '#BTCUSD', 'BTC/USD', 'BTCUSD_raw' -> 'BTCUSD'."""
    up = str(symbol).upper().strip()
    for junk in ("_RAW", "_ECN", "-RAW", ".RAW", ".PRO", ".A", ".B", ".C", ".M", "MICRO", "MINI"):
        if up.endswith(junk):
            up = up[: -len(junk)]
    return "".join(c for c in up if c.isalnum())


def crypto_pair(symbol):
    """The Binance pair for a broker symbol, or None if it is not a crypto we know."""
    return CRYPTO.get(bare(symbol))

# our timeframe -> (yahoo interval, yahoo range, how many yahoo bars make one of ours)
YF_TF = {
    "1m": ("1m", "1d", 1), "5m": ("5m", "5d", 1), "15m": ("15m", "1mo", 1),
    "30m": ("30m", "1mo", 1), "1h": ("1h", "3mo", 1),
    "4h": ("1h", "6mo", 4),      # Yahoo has no 4h bar: fold four 1h bars into one
    "1d": ("1d", "2y", 1),
}
BINANCE_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}

_cache = {}


# Some hosts (Oracle Cloud among them) resolve API names to IPv6 addresses they cannot
# actually route, which fails as a connection error rather than anything readable.
# FORCE_IPV4=1 binds outgoing connections to the IPv4 stack.
def _transport():
    if os.environ.get("FORCE_IPV4", "0") in ("1", "true", "yes"):
        return httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return None


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


def sane_offset_limit(broker_price):
    """How far apart the broker and the free feed may plausibly be, on THIS instrument.

    A flat $120 was a gold number. On bitcoin near $100,000 a $120 gap is ordinary and
    would have been thrown away as implausible, leaving the feed forever uncalibrated;
    on gold a relative limit alone would be far too loose. So: the old floor, or 0.4% of
    price, whichever is larger. It can only widen, never reject what it used to accept.
    """
    try:
        return max(MAX_SANE_OFFSET, abs(float(broker_price)) * 0.004)
    except (TypeError, ValueError):
        return MAX_SANE_OFFSET


def note_broker_price(symbol, broker_price, free_price):
    """Called while MetaApi is awake: learn how far the free feed sits from the broker.

    Kept per instrument. One shared number was fine while the bot only ever traded gold,
    but switching the symbol would then have applied gold's offset to bitcoin - a silent
    corruption of every price the bot reads while the broker is throttled.
    """
    if not (broker_price and free_price):
        return
    diff = float(broker_price) - float(free_price)
    if abs(diff) > sane_offset_limit(broker_price):
        log.warning(f"ignoring implausible calibration diff {diff:.2f} on {symbol}")
        return
    st = _load_state()
    key = bare(symbol)
    book = st.setdefault("bysym", {})
    row = book.setdefault(key, {"samples": []})
    row["samples"] = (row.get("samples", []) + [round(diff, 3)])[-40:]
    row["offset"] = round(statistics.median(row["samples"]), 3)
    row["updated"] = int(time.time())
    if key in {bare(g) for g in GOLD_SYMBOLS}:   # keep the legacy gold fields in step
        st["samples"], st["offset"], st["updated"] = row["samples"], row["offset"], row["updated"]
    _save_state(st)


def _row(symbol=None):
    st = _load_state()
    if symbol:
        key = bare(symbol)
        row = (st.get("bysym") or {}).get(key)
        if row and row.get("samples"):
            return row
        # Nothing stored yet for this symbol. Gold inherits the pre-per-symbol state;
        # anything else starts uncalibrated rather than borrowing gold's number.
        if key in {bare(g) for g in GOLD_SYMBOLS} and st.get("samples"):
            return st
        return {"samples": []}
    return st


def offset(symbol=None):
    row = _row(symbol)
    return float(row.get("offset", 0.0)) if row.get("samples") else 0.0


def calibration_line(symbol=None):
    row = _row(symbol)
    n = len(row.get("samples", []))
    if not n:
        return "📡 مصدر مجاني (غير معاير بعد — سيُعاير تلقائياً أول ما يشتغل الحساب)"
    return f"📡 مصدر مجاني معاير على سعر وسيطك (فرق `{row.get('offset', 0.0)}$` من {n} قياس)"


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


async def _binance(client, pair, timeframe, limit):
    """Binance spot klines, in the shape MetaApi returns."""
    r = await client.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": pair, "interval": BINANCE_TF.get(timeframe, "1h"),
                                 "limit": min(limit, 1000)})
    r.raise_for_status()
    return [{"time": int(k[0] / 1000), "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "tickVolume": float(k[5])} for k in r.json()]


async def _binance_paxg(client, timeframe, limit):
    """PAXG is 1 token = 1 troy ounce of gold, and crypto never closes."""
    return await _binance(client, "PAXGUSDT", timeframe, limit)


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
    ttl = FAST_CACHE_SECS if timeframe in ("1m", "5m") else CACHE_SECS
    if hit and time.time() - hit[0] < ttl:
        return hit[1]

    sym = symbol.upper()
    is_gold = sym in GOLD_SYMBOLS or sym.startswith("XAU")
    pair = crypto_pair(sym)
    errors = []
    async with httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True, transport=_transport()) as client:
        attempts = []
        if sym in YAHOO:
            attempts.append(("yahoo", lambda: _yahoo(client, YAHOO[sym], timeframe, limit)))
        if sym in YAHOO_BACKUP:
            attempts.append(("yahoo-futures", lambda: _yahoo(client, YAHOO_BACKUP[sym], timeframe, limit)))
        if pair:
            attempts.append((f"binance-{pair}", lambda: _binance(client, pair, timeframe, limit)))
            yh = CRYPTO_YAHOO.get(pair)
            if yh:
                attempts.append(("yahoo-crypto", lambda: _yahoo(client, yh, timeframe, limit)))
        if is_gold:
            attempts.append(("binance-paxg", lambda: _binance_paxg(client, timeframe, limit)))
            attempts.append(("stooq", lambda: _stooq_daily(client, "xauusd", limit)))
        if not attempts:  # unknown symbol: try it on Yahoo as-is
            attempts.append(("yahoo-raw", lambda: _yahoo(client, sym, timeframe, limit)))

        for name, fn in attempts:
            try:
                candles = await fn()
                if candles and len(candles) >= 30:
                    if apply_offset:
                        off = offset(sym)
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


async def get_price_fast(symbol="GOLD", spread=0.30):
    """Bid/ask from the last ONE-MINUTE close, calibrated to the broker.

    get_price() reads 15m candles, which is far too stale to trail a $0.50 stop with.
    1m bars are cached for FAST_CACHE_SECS, so this is roughly a 15-second-old price
    for the cost of nothing - and it keeps working when the broker's API is throttled,
    which is the whole point: a bot that can still see the price can still move a stop,
    and moving a stop is a WRITE, billed on a different budget from the reads.
    """
    candles = await get_candles(symbol, "1m", 5)
    if not candles:
        raise RuntimeError("no free candles")
    last = float(candles[-1]["close"])
    half = max(0.01, float(spread) / 2.0)
    return {"bid": round(last - half, 2), "ask": round(last + half, 2),
            "free": True, "at": int(time.time())}


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
