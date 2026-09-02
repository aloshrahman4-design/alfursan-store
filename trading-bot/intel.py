"""
intel.py - market intelligence layer for the Range Harvester bot.

Adds, without touching the trading engine:
  * multi-timeframe technical snapshot (trend, RSI, support/resistance, liquidity zones)
  * cross-market context (USD proxy, equities) when the broker offers those symbols
  * news watcher: polls Finnhub, filters gold-relevant headlines, asks Claude for impact
  * daily trade idea: one setup per day with entry / stop loss / target / confidence

This module NEVER places or closes orders. It only reads data and produces text.

Required env:
  ANTHROPIC_API_KEY   - console.anthropic.com  (paid, per-use)
  FINNHUB_API_KEY     - finnhub.io (free tier is enough)
Optional env:
  CLAUDE_MODEL            default claude-opus-5
  NEWS_POLL_MINUTES       default 10
  NEWS_MAX_ANALYSES_DAY   default 12   (cost guard: Claude news calls per day)
  DAILY_TRADE_HOUR_UTC    default 6    (09:00 Baghdad)
  EXTRA_SYMBOLS           default "EURUSD,US500"
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

try:
    import anthropic
except ImportError:  # keeps the core bot importable even if the SDK is missing
    anthropic = None

log = logging.getLogger("harvester.intel")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5").strip()
NEWS_POLL_MINUTES = int(os.environ.get("NEWS_POLL_MINUTES", "10"))
NEWS_MAX_ANALYSES_DAY = int(os.environ.get("NEWS_MAX_ANALYSES_DAY", "12"))
DAILY_TRADE_HOUR_UTC = int(os.environ.get("DAILY_TRADE_HOUR_UTC", "6"))
EXTRA_SYMBOLS = [s.strip() for s in os.environ.get("EXTRA_SYMBOLS", "EURUSD,US500").split(",") if s.strip()]

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intel_state.json")

TIMEFRAMES = ("15m", "1h", "4h")

# ---------------------------------------------------------------- pure math

def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def calc_rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def find_swings(highs, lows, lookback=5):
    """Swing highs / swing lows: a bar that is the extreme of its neighbourhood."""
    swing_highs, swing_lows = [], []
    for i in range(lookback, len(lows) - lookback):
        if lows[i] == min(lows[i - lookback:i + lookback + 1]):
            swing_lows.append(lows[i])
        if highs[i] == max(highs[i - lookback:i + lookback + 1]):
            swing_highs.append(highs[i])
    return swing_highs, swing_lows


def cluster_levels(levels, tolerance):
    """Group nearby levels. A cluster touched 2+ times is where resting orders
    (stops / limit orders) pile up - what traders call a liquidity zone."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters, current = [], [levels[0]]
    for lvl in levels[1:]:
        if lvl - current[-1] <= tolerance:
            current.append(lvl)
        else:
            clusters.append(current)
            current = [lvl]
    clusters.append(current)
    out = [{"price": round(sum(c) / len(c), 2), "touches": len(c)} for c in clusters]
    return sorted(out, key=lambda z: -z["touches"])


def analyze_timeframe(candles):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    price = closes[-1]
    ema_fast = calc_ema(closes, 50)
    ema_slow = calc_ema(closes, min(200, len(closes) - 1))
    rsi = calc_rsi(closes, 14)

    if ema_fast and ema_slow:
        trend = "up" if ema_fast > ema_slow else "down"
    elif ema_fast:
        trend = "up" if price > ema_fast else "down"
    else:
        trend = "flat"

    swing_highs, swing_lows = find_swings(highs, lows)
    tol = max(price * 0.0006, 0.5)  # ~0.06% of price (≈2.5$ on gold at 4400)
    res_zones = [z for z in cluster_levels(swing_highs, tol) if z["price"] > price]
    sup_zones = [z for z in cluster_levels(swing_lows, tol) if z["price"] < price]
    near_res = min(res_zones, key=lambda z: z["price"], default=None)
    near_sup = max(sup_zones, key=lambda z: z["price"], default=None)
    liquidity = [z for z in res_zones + sup_zones if z["touches"] >= 2][:4]

    rng_high, rng_low = max(highs[-40:]), min(lows[-40:])
    return {
        "price": round(price, 2),
        "trend": trend,
        "rsi": round(rsi, 1) if rsi is not None else None,
        "ema_fast": round(ema_fast, 2) if ema_fast else None,
        "ema_slow": round(ema_slow, 2) if ema_slow else None,
        "near_support": near_sup,
        "near_resistance": near_res,
        "liquidity_zones": liquidity,
        "range_high": round(rng_high, 2),
        "range_low": round(rng_low, 2),
    }


def pct_change(candles, bars):
    if len(candles) <= bars:
        return None
    a, b = candles[-bars - 1]["close"], candles[-1]["close"]
    return round((b - a) / a * 100, 2) if a else None


# ---------------------------------------------------------------- snapshot

async def build_snapshot(get_candles, symbol):
    """get_candles(symbol, timeframe, limit) -> list of candle dicts (async)."""
    snap = {"symbol": symbol, "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "tf": {}, "markets": {}}
    for tf in TIMEFRAMES:
        try:
            candles = await get_candles(symbol, tf, 200)
            if candles and len(candles) >= 60:
                snap["tf"][tf] = analyze_timeframe(candles)
                snap["tf"][tf]["chg_24h"] = pct_change(candles, {"15m": 96, "1h": 24, "4h": 6}[tf])
        except Exception as e:
            log.warning(f"snapshot {tf} failed: {e}")
    for sym in EXTRA_SYMBOLS:
        try:
            candles = await get_candles(sym, "1h", 120)
            if candles and len(candles) >= 30:
                snap["markets"][sym] = {
                    "price": round(candles[-1]["close"], 4),
                    "trend": analyze_timeframe(candles)["trend"],
                    "chg_24h": pct_change(candles, 24),
                }
        except Exception as e:
            log.info(f"extra symbol {sym} unavailable: {e}")
    return snap


AR_TREND = {"up": "صاعد 📈", "down": "هابط 📉", "flat": "عرضي ➡️"}


def rsi_words(rsi):
    if rsi is None:
        return "غير متوفر"
    if rsi >= 70:
        return f"{rsi} — تشبّع شراء (احتمال تراجع)"
    if rsi <= 30:
        return f"{rsi} — تشبّع بيع (احتمال ارتداد)"
    return f"{rsi} — محايد"


def snapshot_to_arabic(snap):
    """Plain-language Arabic summary. No Claude needed - always available."""
    lines = [f"📊 *صورة السوق الآن* ({snap['symbol']}, {snap['utc']} UTC)"]
    tf_names = {"15m": "١٥ دقيقة", "1h": "ساعة", "4h": "٤ ساعات"}
    for tf in TIMEFRAMES:
        d = snap["tf"].get(tf)
        if not d:
            continue
        lines.append(f"\n⏱ *فريم {tf_names[tf]}* — السعر {d['price']}")
        lines.append(f"• الاتجاه: {AR_TREND[d['trend']]}  | RSI: {rsi_words(d['rsi'])}")
        if d["near_support"]:
            lines.append(f"• أقرب دعم: {d['near_support']['price']} (لمسات: {d['near_support']['touches']})")
        if d["near_resistance"]:
            lines.append(f"• أقرب مقاومة: {d['near_resistance']['price']} (لمسات: {d['near_resistance']['touches']})")
        if d["liquidity_zones"]:
            zones = "، ".join(f"{z['price']} ×{z['touches']}" for z in d["liquidity_zones"])
            lines.append(f"• مناطق سيولة (تجمّع أوامر): {zones}")
        lines.append(f"• نطاق آخر ٤٠ شمعة: {d['range_low']} → {d['range_high']}")

    if snap["markets"]:
        lines.append("\n🌍 *أسواق مرتبطة:*")
        for sym, m in snap["markets"].items():
            note = ""
            if sym.upper().startswith("EURUSD"):
                note = " — اليورو يصعد = الدولار يضعف = داعم للذهب" if m["trend"] == "up" else " — الدولار يقوى = ضاغط على الذهب"
            elif "500" in sym or "NAS" in sym.upper():
                note = " — شهية مخاطرة عالية" if m["trend"] == "up" else " — هروب من المخاطرة (يدعم الذهب غالباً)"
            chg = f" ({m['chg_24h']:+}% باليوم)" if m["chg_24h"] is not None else ""
            lines.append(f"• {sym}: {AR_TREND[m['trend']]}{chg}{note}")

    lines.append("\n⚠️ تحليل احتمالي، ليس توصية مضمونة.")
    return "\n".join(lines)


# ---------------------------------------------------------------- news

GOLD_KEYWORDS = {
    "gold": 3, "xau": 3, "bullion": 3, "precious metal": 3, "safe haven": 2, "safe-haven": 2,
    "fed": 2, "fomc": 2, "powell": 2, "rate cut": 2, "rate hike": 2, "interest rate": 2,
    "inflation": 2, "cpi": 2, "pce": 2, "ppi": 1, "nonfarm": 2, "non-farm": 2, "payroll": 2,
    "jobs report": 2, "unemployment": 1, "gdp": 1, "treasury": 1, "yield": 1, "dollar": 2,
    "dxy": 2, "geopolit": 2, "war": 1, "ceasefire": 1, "tariff": 2, "sanction": 1,
    "central bank": 2, "ecb": 1, "boj": 1, "pboc": 1, "risk-off": 2, "recession": 2, "missile": 1, "strike": 1,
}


def relevance(headline, summary=""):
    text = f"{headline} {summary}".lower()
    return sum(w for k, w in GOLD_KEYWORDS.items() if k in text)


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "analyses": {}, "last_daily": ""}


def _save_state(state):
    try:
        state["seen"] = state["seen"][-600:]
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"state save failed: {e}")


GOOGLE_NEWS_FEEDS = (
    "https://news.google.com/rss/search?q=gold+price+OR+XAUUSD+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=federal+reserve+OR+inflation+OR+dollar+when:1d&hl=en-US&gl=US&ceid=US:en",
)

_RSS_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_RSS_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_RSS_DATE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_RSS_LINK = re.compile(r"<link>(.*?)</link>", re.S)
_RSS_SOURCE = re.compile(r"<source[^>]*>(.*?)</source>", re.S)


def _unescape(text):
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        text = text.replace(a, b)
    return re.sub(r"<[^>]+>", "", text).strip()


async def fetch_news_free(limit=40):
    """Headlines with no API key at all (Google News RSS). Fallback when Finnhub is absent."""
    items = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; RangeHarvester/1.0)"}) as client:
        for url in GOOGLE_NEWS_FEEDS:
            try:
                r = await client.get(url)
                r.raise_for_status()
                for raw in _RSS_ITEM.findall(r.text)[:40]:
                    tm = _RSS_TITLE.search(raw)
                    if not tm:
                        continue
                    headline = _unescape(tm.group(1))
                    score = relevance(headline)
                    if score < 2:
                        continue
                    ts = 0
                    dm = _RSS_DATE.search(raw)
                    if dm:
                        try:
                            ts = int(parsedate_to_datetime(dm.group(1).strip()).timestamp())
                        except Exception:
                            ts = 0
                    sm = _RSS_SOURCE.search(raw)
                    lm = _RSS_LINK.search(raw)
                    items.append({
                        "id": None,
                        "ts": ts or int(time.time()),
                        "headline": headline,
                        "summary": "",
                        "source": _unescape(sm.group(1)) if sm else "Google News",
                        "url": lm.group(1).strip() if lm else "",
                        "score": score,
                    })
            except Exception as e:
                log.warning(f"free news feed failed: {e}")
    uniq = {it["headline"]: it for it in items}
    return sorted(uniq.values(), key=lambda x: -x["ts"])[:limit]


async def fetch_news(limit=40):
    """Gold-relevant headlines from Finnhub (forex + general), newest first."""
    if not FINNHUB_API_KEY:
        return await fetch_news_free(limit)
    items = []
    async with httpx.AsyncClient(timeout=15) as client:
        for cat in ("forex", "general"):
            try:
                r = await client.get("https://finnhub.io/api/v1/news", params={"category": cat, "token": FINNHUB_API_KEY})
                r.raise_for_status()
                for it in r.json():
                    score = relevance(it.get("headline", ""), it.get("summary", ""))
                    if score >= 2:
                        items.append({
                            "id": it.get("id"),
                            "ts": it.get("datetime", 0),
                            "headline": it.get("headline", "").strip(),
                            "summary": (it.get("summary") or "").strip()[:300],
                            "source": it.get("source", ""),
                            "url": it.get("url", ""),
                            "score": score,
                        })
            except Exception as e:
                log.warning(f"finnhub {cat} failed: {e}")
    if not items:
        return await fetch_news_free(limit)
    uniq = {}
    for it in items:
        uniq[it["headline"]] = it
    return sorted(uniq.values(), key=lambda x: -x["ts"])[:limit]


def news_to_arabic(items):
    if not items:
        return "📰 لا توجد أخبار مؤثرة على الذهب حالياً."
    lines = ["📰 *آخر الأخبار المؤثرة على الذهب:*"]
    for it in items[:8]:
        t = datetime.fromtimestamp(it["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        lines.append(f"• [{t} UTC] {it['headline']}  _({it['source']})_")
    return "\n".join(lines)


# ---------------------------------------------------------------- Claude

SYSTEM_PROMPT = (
    "أنت محلل متخصص بسوق الذهب (XAUUSD) تكتب لمتداول عراقي بالعربية البسيطة والمباشرة.\n"
    "قواعدك:\n"
    "- لا تدّعي اليقين أبداً؛ كل شيء احتمالات، واذكر ما الذي يُلغي السيناريو.\n"
    "- استخدم أرقاماً محددة (مستويات، أهداف، وقف خسارة) مأخوذة من البيانات المعطاة.\n"
    "- اختصر: نقاط قصيرة، بدون مقدمات، بدون تكرار البيانات الخام.\n"
    "- عندما تكون الصورة غير واضحة قل 'لا صفقة' بوضوح؛ الحفاظ على رأس المال أهم من التداول.\n"
    "- الأخبار: اذكر الأثر المباشر على الذهب (صعود/هبوط/محايد)، قوته (ضعيف/متوسط/قوي)، ومدته (دقائق/ساعات/أيام).\n"
)

_client = None


def refresh_keys():
    """Re-read keys from the environment (used after /setkey) and drop the cached client."""
    global ANTHROPIC_API_KEY, FINNHUB_API_KEY, CLAUDE_MODEL, _client
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5").strip()
    _client = None
    return {"claude": claude_ready(), "news": True}


def keys_status():
    def mask(v):
        return f"{v[:10]}…{v[-4:]}" if len(v) > 16 else ("مضبوط" if v else "غير مضبوط")
    return (
        f"🔑 *حالة المفاتيح*\n"
        f"• Claude: {'✅ ' + mask(ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else '❌ غير مضبوط'}\n"
        f"• Finnhub: {'✅ ' + mask(FINNHUB_API_KEY) if FINNHUB_API_KEY else '⚪ غير مضبوط (الأخبار تشتغل من المصدر المجاني)'}\n"
        f"• النموذج: `{CLAUDE_MODEL}`"
    )


def claude_ready():
    return bool(ANTHROPIC_API_KEY) and anthropic is not None


def _client_or_none():
    global _client
    if not claude_ready():
        return None
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=2, timeout=120.0)
    return _client


def _text_of(response):
    if response.stop_reason == "refusal":
        return "⚠️ النموذج رفض الطلب (سياسة أمان). حاول لاحقاً."
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def _ask(user_text, effort="medium", max_tokens=1500, schema=None):
    client = _client_or_none()
    if client is None:
        return None
    output_config = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    response = await client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        output_config=output_config,
        messages=[{"role": "user", "content": user_text}],
    )
    return _text_of(response)


async def claude_news_impact(items, snap):
    heads = "\n".join(f"- {it['headline']} ({it['source']}): {it['summary']}" for it in items[:10])
    tf1h = snap["tf"].get("1h") or {}
    ctx = f"السعر الآن {tf1h.get('price')}، اتجاه الساعة {tf1h.get('trend')}، RSI {tf1h.get('rsi')}, دعم {tf1h.get('near_support')}, مقاومة {tf1h.get('near_resistance')}"
    prompt = (
        f"أخبار جديدة وصلت الآن:\n{heads}\n\nحالة السوق: {ctx}\n\n"
        "أعطني: (1) الخبر الأهم وتأثيره المباشر على الذهب، (2) الاتجاه المرجّح للساعات القادمة مع نسبة ثقة، "
        "(3) مستوى واحد يجب مراقبته، (4) ماذا يُلغي هذا التوقع. بحد أقصى 8 أسطر."
    )
    return await _ask(prompt, effort="medium", max_tokens=1200)


async def claude_market_brief(snap, items):
    heads = "\n".join(f"- {it['headline']}" for it in items[:6]) or "لا أخبار مؤثرة"
    prompt = (
        f"بيانات فنية (JSON):\n{json.dumps(snap, ensure_ascii=False)}\n\nأخبار آخر ساعات:\n{heads}\n\n"
        "اشرح لي بلغة بسيطة جداً: أين السعر بالنسبة للدعوم والمقاومات، أين السيولة، شنو الأسواق الثانية تقول، "
        "وشنو السيناريو الأرجح مع ما يُلغيه. 10 أسطر كحد أقصى."
    )
    return await _ask(prompt, effort="medium", max_tokens=1500)


TRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["buy", "sell", "no_trade"]},
        "entry": {"type": "number"},
        "stop_loss": {"type": "number"},
        "take_profit": {"type": "number"},
        "confidence": {"type": "integer"},
        "timeframe": {"type": "string"},
        "reasoning_ar": {"type": "string"},
        "invalidation_ar": {"type": "string"},
    },
    "required": ["direction", "entry", "stop_loss", "take_profit", "confidence", "timeframe", "reasoning_ar", "invalidation_ar"],
    "additionalProperties": False,
}


async def claude_daily_trade(snap, items):
    heads = "\n".join(f"- {it['headline']}: {it['summary']}" for it in items[:10]) or "لا أخبار مؤثرة"
    prompt = (
        f"بيانات فنية متعددة الأطر (JSON):\n{json.dumps(snap, ensure_ascii=False)}\n\n"
        f"أخبار آخر 24 ساعة:\n{heads}\n\n"
        "اقترح صفقة واحدة فقط لليوم على الذهب (أو no_trade إذا الصورة غير واضحة). "
        "الدخول والوقف والهدف يجب أن تكون أرقاماً من مستويات الدعم/المقاومة/السيولة المعطاة، "
        "ونسبة العائد/المخاطرة لا تقل عن 1.5. confidence من 0 إلى 100."
    )
    raw = await _ask(prompt, effort="high", max_tokens=2000, schema=TRADE_SCHEMA)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"direction": "no_trade", "entry": 0, "stop_loss": 0, "take_profit": 0, "confidence": 0,
                "timeframe": "", "reasoning_ar": raw[:800], "invalidation_ar": ""}


def trade_to_arabic(t):
    if not t:
        return "⚠️ تعذّر توليد صفقة اليوم (تحقق من مفتاح Claude)."
    if t["direction"] == "no_trade":
        return f"🎯 *صفقة اليوم: لا صفقة*\n{t['reasoning_ar']}\n\n⚠️ الانتظار قرار أيضاً."
    arrow = "🟢 شراء (BUY)" if t["direction"] == "buy" else "🔴 بيع (SELL)"
    risk = abs(t["entry"] - t["stop_loss"])
    reward = abs(t["take_profit"] - t["entry"])
    rr = round(reward / risk, 2) if risk else 0
    return (
        f"🎯 *صفقة اليوم — {arrow}*\n"
        f"• الدخول: `{t['entry']}`\n"
        f"• وقف الخسارة: `{t['stop_loss']}`  (مخاطرة {round(risk,2)}$)\n"
        f"• الهدف: `{t['take_profit']}`  (عائد {round(reward,2)}$، نسبة {rr}:1)\n"
        f"• الثقة: {t['confidence']}%  | الإطار: {t['timeframe']}\n\n"
        f"📌 السبب: {t['reasoning_ar']}\n"
        f"🚫 يُلغى إذا: {t['invalidation_ar']}\n\n"
        f"⚠️ فكرة للتقييم، القرار والتنفيذ لك. لا تخاطر بأكثر من 1-2% من الحساب."
    )


# ---------------------------------------------------------------- background tasks

async def news_watcher(get_candles, symbol, notify):
    """Poll Finnhub; when new gold-relevant headlines appear, alert + Claude impact."""
    state = _load_state()
    first_run = True
    while True:
        try:
            items = await fetch_news()
            fresh = [it for it in items if it["headline"] not in state["seen"] and it["score"] >= 3]
            for it in items:
                if it["headline"] not in state["seen"]:
                    state["seen"].append(it["headline"])
            if first_run:
                first_run = False  # don't spam old headlines at boot
                fresh = []
            if fresh:
                await notify(news_to_arabic(fresh))
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                used = state["analyses"].get(today, 0)
                if claude_ready() and used < NEWS_MAX_ANALYSES_DAY:
                    snap = await build_snapshot(get_candles, symbol)
                    verdict = await claude_news_impact(fresh, snap)
                    if verdict:
                        await notify("🧠 *تأثير الخبر على الذهب:*\n" + verdict)
                        state["analyses"] = {today: used + 1}
            _save_state(state)
        except Exception as e:
            log.warning(f"news_watcher error: {e}")
        await asyncio.sleep(NEWS_POLL_MINUTES * 60)


async def daily_scheduler(get_candles, symbol, notify):
    """Once per day at DAILY_TRADE_HOUR_UTC: post the daily trade idea."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            state = _load_state()
            today = now.strftime("%Y-%m-%d")
            if now.hour >= DAILY_TRADE_HOUR_UTC and state.get("last_daily") != today:
                msg = await daily_trade_message(get_candles, symbol)
                await notify(msg)
                state["last_daily"] = today
                _save_state(state)
        except Exception as e:
            log.warning(f"daily_scheduler error: {e}")
        await asyncio.sleep(300)


async def daily_trade_message(get_candles, symbol):
    snap = await build_snapshot(get_candles, symbol)
    items = await fetch_news()
    trade = await claude_daily_trade(snap, items)
    return trade_to_arabic(trade)


async def market_brief_message(get_candles, symbol):
    snap = await build_snapshot(get_candles, symbol)
    text = snapshot_to_arabic(snap)
    if claude_ready():
        items = await fetch_news()
        brief = await claude_market_brief(snap, items)
        if brief:
            text += "\n\n🧠 *قراءة Claude:*\n" + brief
    return text


async def news_now_message(get_candles, symbol):
    items = await fetch_news()
    text = news_to_arabic(items)
    if items and claude_ready():
        snap = await build_snapshot(get_candles, symbol)
        verdict = await claude_news_impact(items, snap)
        if verdict:
            text += "\n\n🧠 *التأثير على الذهب:*\n" + verdict
    return text
