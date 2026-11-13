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
PAPER_TF = os.environ.get("PAPER_TF", "1m")
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
    return {"balance": START_BALANCE, "positions": [], "orders": [], "history": [], "next_id": 1,
            "since": datetime.now(timezone.utc).strftime("%Y-%m-%d")}


def _load():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
            state.setdefault("history", [])
            state.setdefault("orders", [])
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

    # Its reads are a local JSON file: no API, no quota, nothing metered. Saying so is
    # what keeps the engines on their fast tick in paper mode - without it they fall back
    # to the slow pace meant for a billed API, and a $2 grid cannot work at 15s a look.
    streaming = True

    async def connect(self):
        state = _load()
        _save(state)
        log.info(f"paper account ready: balance {state['balance']}")
        return self

    async def close(self):
        return None

    async def _mid(self):
        # One-minute bars, not fifteen. A $2 grid step cannot be tested against a price
        # that only moves every quarter of an hour: fills, targets and trailing stops all
        # land on a price that never existed, and the result is a confident fiction.
        for tf in (PAPER_TF, "5m", "15m"):
            try:
                candles = await datafeed.get_candles(ENGINE_SYMBOL, tf, 60)
            except Exception:
                candles = None
            if candles:
                return float(candles[-1]["close"])
        raise RuntimeError("no price available for the paper account")

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
        await self._trigger_pending()
        await self._trigger_take_profits()
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

    async def close_position_partially(self, position_id, volume, options=None):
        """Book part of a position and shrink what stays open, keeping the same id."""
        state = _load()
        price = await self._mid()
        for p in state["positions"]:
            if p["id"] != position_id:
                continue
            vol = float(p.get("volume", 0) or 0)
            cut = round(min(float(volume), vol), 2)
            left = round(vol - cut, 2)
            if cut <= 0 or left < 0:
                raise RuntimeError("invalid partial volume")
            if left == 0:
                return await self.close_position(position_id)
            booked = round(profit_of(p, price) * (cut / vol), 2)
            p["volume"] = left
            state["balance"] = round(state.get("balance", 0.0) + booked, 2)
            state.setdefault("closed", []).append(
                {"id": p["id"], "volume": cut, "profit": booked, "partial": True})
            _save(state)
            log.info(f"paper partial close {position_id}: {cut} booked {booked}")
            return {"orderId": p["id"], "volume": cut, "profit": booked}
        raise RuntimeError(f"no such position {position_id}")

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

    # --- pending stop orders -----------------------------------------------------------
    # The grid runs on stop orders, and a strategy this dangerous has to be testable for
    # free before it is testable with money. Fills are checked whenever positions are read,
    # which the engine does about once a second.
    async def _trigger_pending(self):
        state = _load()
        if not state.get("orders"):
            return
        try:
            mid = await self._mid()
        except Exception:
            return
        ask, bid = mid + SPREAD / 2, mid - SPREAD / 2
        left, fired = [], []
        for o in state["orders"]:
            hit = (ask >= o["openPrice"]) if o["type"] == "ORDER_TYPE_BUY_STOP" else (bid <= o["openPrice"])
            (fired if hit else left).append(o)
        if not fired:
            return
        for o in fired:
            state["positions"].append({
                "id": o["id"],
                "symbol": ENGINE_SYMBOL,
                "type": "POSITION_TYPE_BUY" if o["type"] == "ORDER_TYPE_BUY_STOP" else "POSITION_TYPE_SELL",
                "magic": o.get("magic", 0),
                "takeProfit": o.get("takeProfit"),
                "openPrice": o["openPrice"],     # a stop fills at its price, slippage aside
                "volume": o["volume"],
                "openedAt": int(time.time()),
            })
            log.info(f"paper pending {o['id']} filled at {o['openPrice']}")
        state["orders"] = left
        _save(state)

    async def _trigger_take_profits(self):
        """Close anything that reached its take profit or its stop loss."""
        state = _load()
        hit = []
        try:
            mid = await self._mid()
        except Exception:
            return
        bid, ask = mid - SPREAD / 2, mid + SPREAD / 2
        for p in state["positions"]:
            is_buy = p["type"] == "POSITION_TYPE_BUY"
            tp, sl = p.get("takeProfit"), p.get("stopLoss")
            if tp and ((bid >= tp) if is_buy else (ask <= tp)):
                hit.append((p, float(tp)))
            elif sl and ((bid <= sl) if is_buy else (ask >= sl)):
                hit.append((p, float(sl)))
        for p, at in hit:
            # A stop or a target executes at its own price, not wherever price has reached
            # by the time we look. Booking it at the current mid made a protected trade
            # report the loss it was protected from.
            pnl = profit_of(p, at)
            state["balance"] = round(state["balance"] + pnl, 2)
            state["positions"] = [q for q in state["positions"] if q["id"] != p["id"]]
            state["history"].append({**p, "closePrice": round(at, 2), "profit": pnl,
                                     "closedAt": int(time.time()), "reason": "TP/SL"})
            log.info(f"paper exit {p['id']} at {at} pnl {pnl}")
        if hit:
            state["history"] = state["history"][-500:]
            _save(state)

    async def _place_pending(self, kind, volume, open_price, options, take_profit=None):
        state = _load()
        order = {
            "id": f"O{state['next_id']}",
            "symbol": ENGINE_SYMBOL,
            "type": kind,
            "magic": (options or {}).get("magic", 0),
            "openPrice": round(float(open_price), 2),
            "takeProfit": round(float(take_profit), 2) if take_profit else None,
            "volume": float(volume or 0.01),
            "placedAt": int(time.time()),
        }
        state["next_id"] += 1
        state.setdefault("orders", []).append(order)
        _save(state)
        return {"orderId": order["id"]}

    async def create_stop_buy_order(self, symbol=None, volume=None, open_price=None,
                                    stop_loss=None, take_profit=None, options=None, **kw):
        return await self._place_pending("ORDER_TYPE_BUY_STOP", volume, open_price, options, take_profit)

    async def create_stop_sell_order(self, symbol=None, volume=None, open_price=None,
                                     stop_loss=None, take_profit=None, options=None, **kw):
        return await self._place_pending("ORDER_TYPE_SELL_STOP", volume, open_price, options, take_profit)

    async def get_orders(self, **kw):
        await self._trigger_pending()
        return [dict(o) for o in _load().get("orders", [])]

    async def cancel_order(self, order_id):
        state = _load()
        before = len(state.get("orders", []))
        state["orders"] = [o for o in state.get("orders", []) if o["id"] != str(order_id)]
        if len(state["orders"]) == before:
            raise RuntimeError(f"paper order {order_id} not found")
        _save(state)
        return {"orderId": str(order_id)}

    async def modify_position(self, position_id, stop_loss=None, take_profit=None, **kw):
        state = _load()
        pos = next((p for p in state["positions"] if p["id"] == str(position_id)), None)
        if pos is None:
            raise RuntimeError(f"paper position {position_id} not found")
        if take_profit is not None:
            pos["takeProfit"] = round(float(take_profit), 2)
        if stop_loss is not None:
            pos["stopLoss"] = round(float(stop_loss), 2)
        _save(state)
        return {"positionId": pos["id"]}

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
