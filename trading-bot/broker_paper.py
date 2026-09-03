"""
broker_paper.py - the bot trades itself, with no broker at all.

Real execution needs a broker, and every regulated broker needs identity verification.
That is the law, not an obstacle to route around. But the *strategy* can be proven
without any of it: this module runs the exact same anchor logic against real live gold
prices and keeps a virtual account, so the user learns whether the approach actually
makes money before spending anything on it.

It mirrors the method names of the MetaApi RPC connection, exactly like broker_capital
does, so the trading engine and its strategy are untouched - only the account is
imaginary. Prices come from datafeed.py (free, keyless, real).

Env:
  PAPER_BALANCE=10000        starting virtual balance
  PAPER_CONTRACT_SIZE=100    units per lot (gold: 1 lot = 100 oz, so 0.03 lot = 3 oz)
  PAPER_SPREAD=0.30          simulated spread in price units
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import datafeed

log = logging.getLogger("harvester.paper")

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json")
START_BALANCE = float(os.environ.get("PAPER_BALANCE", "10000"))
CONTRACT_SIZE = float(os.environ.get("PAPER_CONTRACT_SIZE", "100"))
SPREAD = float(os.environ.get("PAPER_SPREAD", "0.30"))
ENGINE_SYMBOL = os.environ.get("SYMBOL", "GOLD").strip()


def refresh_keys():
    """Kept for symmetry with the other brokers; paper trading has nothing to configure."""
    global START_BALANCE, CONTRACT_SIZE, SPREAD, ENGINE_SYMBOL
    START_BALANCE = float(os.environ.get("PAPER_BALANCE", "10000"))
    CONTRACT_SIZE = float(os.environ.get("PAPER_CONTRACT_SIZE", "100"))
    SPREAD = float(os.environ.get("PAPER_SPREAD", "0.30"))
    ENGINE_SYMBOL = os.environ.get("SYMBOL", "GOLD").strip()


def configured():
    return True


def _blank():
    return {"balance": START_BALANCE, "positions": [], "history": [], "next_id": 1,
            "since": datetime.now(timezone.utc).strftime("%Y-%m-%d")}


def _load():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
            state.setdefault("history", [])
            state.setdefault("next_id", 1)
            return state
    except Exception:
        return _blank()


def _save(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.warning(f"paper state save failed: {e}")


def reset():
    _save(_blank())


def profit_of(pos, price):
    """Cash P/L of one position at the given mid price, in account currency."""
    direction = 1 if pos["type"] == "POSITION_TYPE_BUY" else -1
    close_price = price - SPREAD / 2 if direction == 1 else price + SPREAD / 2
    return round((close_price - pos["openPrice"]) * direction * pos["volume"] * CONTRACT_SIZE, 2)


class PaperBroker:
    """Same surface as the real brokers, backed by a JSON file instead of a broker."""

    async def connect(self):
        state = _load()
        _save(state)
        log.info(f"paper account ready: balance {state['balance']}")
        return self

    async def close(self):
        return None

    async def _mid(self):
        candles = await datafeed.get_candles(ENGINE_SYMBOL, "15m", 60)
        if not candles:
            raise RuntimeError("no price available for the paper account")
        return float(candles[-1]["close"])

    async def get_symbol_price(self, symbol=None):
        mid = await self._mid()
        return {"bid": round(mid - SPREAD / 2, 2), "ask": round(mid + SPREAD / 2, 2)}

    async def get_account_information(self):
        state = _load()
        try:
            mid = await self._mid()
            floating = sum(profit_of(p, mid) for p in state["positions"])
        except Exception:
            floating = 0.0
        return {"balance": round(state["balance"], 2),
                "equity": round(state["balance"] + floating, 2), "currency": "USD"}

    async def get_positions(self):
        state = _load()
        try:
            mid = await self._mid()
        except Exception:
            mid = None
        out = []
        for p in state["positions"]:
            item = dict(p)
            item["unrealizedProfit"] = profit_of(p, mid) if mid else 0.0
            out.append(item)
        return out

    async def _open(self, direction, volume, options):
        state = _load()
        mid = await self._mid()
        entry = mid + SPREAD / 2 if direction == "BUY" else mid - SPREAD / 2
        pos = {
            "id": f"P{state['next_id']}",
            "symbol": ENGINE_SYMBOL,
            "type": "POSITION_TYPE_BUY" if direction == "BUY" else "POSITION_TYPE_SELL",
            "magic": (options or {}).get("magic", 0),
            "openPrice": round(entry, 2),
            "volume": float(volume or 0.03),
            "openedAt": int(time.time()),
        }
        state["next_id"] += 1
        state["positions"].append(pos)
        _save(state)
        log.info(f"paper open {direction} {pos['volume']} @ {pos['openPrice']} (id {pos['id']})")
        return {"dealId": pos["id"], "openPrice": pos["openPrice"]}

    async def create_market_buy_order(self, symbol=None, volume=None, options=None):
        return await self._open("BUY", volume, options)

    async def create_market_sell_order(self, symbol=None, volume=None, options=None):
        return await self._open("SELL", volume, options)

    async def close_position(self, position_id):
        state = _load()
        pos = next((p for p in state["positions"] if p["id"] == str(position_id)), None)
        if pos is None:
            raise RuntimeError(f"paper position {position_id} not found")
        mid = await self._mid()
        pnl = profit_of(pos, mid)
        state["balance"] = round(state["balance"] + pnl, 2)
        state["positions"] = [p for p in state["positions"] if p["id"] != pos["id"]]
        state["history"].append({**pos, "closePrice": round(mid, 2), "profit": pnl,
                                 "closedAt": int(time.time())})
        state["history"] = state["history"][-500:]
        _save(state)
        log.info(f"paper close {pos['id']} pnl {pnl}")
        return {"dealId": pos["id"], "profit": pnl}

    async def get_candles(self, symbol=None, timeframe="1h", limit=200):
        return await datafeed.get_candles(symbol or ENGINE_SYMBOL, timeframe, limit)

    # no-ops the engine calls on a MetaApi connection
    async def wait_synchronized(self, **kw):
        return True

    async def subscribe_to_market_data(self, **kw):
        return True


def report():
    """Arabic performance summary: the whole point of paper trading is this number."""
    state = _load()
    hist = state.get("history", [])
    if not hist:
        return (f"📄 *الحساب التجريبي الداخلي*\n"
                f"• الرصيد: `{round(state['balance'], 2)}$`\n"
                f"• صفقات مفتوحة: `{len(state.get('positions', []))}`\n"
                f"• ما تم إغلاق أي صفقة بعد.")
    wins = [t for t in hist if t["profit"] > 0]
    losses = [t for t in hist if t["profit"] <= 0]
    total = round(sum(t["profit"] for t in hist), 2)
    best = max(t["profit"] for t in hist)
    worst = min(t["profit"] for t in hist)
    avg_win = round(sum(t["profit"] for t in wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(t["profit"] for t in losses) / len(losses), 2) if losses else 0
    growth = round((state["balance"] - START_BALANCE) / START_BALANCE * 100, 2) if START_BALANCE else 0
    verdict = ("✅ الاستراتيجية رابحة على هذي العينة" if total > 0
               else "❌ الاستراتيجية خاسرة على هذي العينة — لا تشغّلها بفلوس حقيقية")
    return (f"📄 *تقرير الحساب التجريبي* (منذ {state.get('since', '')})\n\n"
            f"• الرصيد: `{round(state['balance'], 2)}$` (البداية {START_BALANCE}$، {growth:+}%)\n"
            f"• صافي الربح: `{total:+}$`\n"
            f"• عدد الصفقات: `{len(hist)}` — رابحة `{len(wins)}` / خاسرة `{len(losses)}`\n"
            f"• نسبة النجاح: `{round(len(wins) / len(hist) * 100)}%`\n"
            f"• متوسط الرابحة: `{avg_win}$` | متوسط الخاسرة: `{avg_loss}$`\n"
            f"• أفضل صفقة: `{best:+}$` | أسوأ صفقة: `{worst:+}$`\n"
            f"• صفقات مفتوحة الآن: `{len(state.get('positions', []))}`\n\n"
            f"{verdict}\n"
            f"⚠️ نتائج تجريبية بأسعار حقيقية، بدون انزلاق سعري أو عمولات الوسيط.")
