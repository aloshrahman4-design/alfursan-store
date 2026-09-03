"""
broker_capital.py - direct execution through Capital.com's free REST API.

Replaces MetaApi entirely: no paid bridge, no deployed-hours bill. Authentication is
an API key plus the account login, and a demo account uses the same code path as a
live one - only the base URL changes.

The class deliberately mirrors the method names of the MetaApi RPC connection the
engine already speaks to (get_symbol_price, get_positions, create_market_buy_order,
close_position, get_account_information), so the trading engine needs no rewrite.

Two things Capital.com does not give us, and how they are handled:
  * No "magic number" on positions. We record every deal we open in a local file and
    attach the magic back on read, so the engine still only ever touches its own trades.
  * No lot sizes. Position size is in the broker's own units, set by CAPITAL_TRADE_SIZE.

Bonus over MetaApi: stopLevel/profitLevel are sent with the order, so the stop loss
lives at the broker and survives the bot being closed.

Env:
  CAPITAL_API_KEY, CAPITAL_EMAIL, CAPITAL_PASSWORD
  CAPITAL_DEMO=1            1 = demo account (default), 0 = live
  CAPITAL_EPIC=GOLD         instrument id; auto-discovered if the default is missing
  CAPITAL_TRADE_SIZE=1      size per order, in the broker's units
  STOP_LOSS_DIST=0          price distance for a real broker-side stop (0 = off)
  TAKE_PROFIT_DIST=0        same for a target

Run it directly to check the connection:  python broker_capital.py
"""

import asyncio
import json
import logging
import os
import time

import httpx

log = logging.getLogger("harvester.capital")

DEMO = os.environ.get("CAPITAL_DEMO", "1") not in ("0", "false", "no")
BASE = ("https://demo-api-capital.backend-capital.com/api/v1" if DEMO
        else "https://api-capital.backend-capital.com/api/v1")
API_KEY = os.environ.get("CAPITAL_API_KEY", "").strip()
EMAIL = os.environ.get("CAPITAL_EMAIL", "").strip()
PASSWORD = os.environ.get("CAPITAL_PASSWORD", "").strip()
EPIC = os.environ.get("CAPITAL_EPIC", "GOLD").strip()
ENGINE_SYMBOL = os.environ.get("SYMBOL", "GOLD").strip()
TRADE_SIZE = float(os.environ.get("CAPITAL_TRADE_SIZE", "1"))
STOP_LOSS_DIST = float(os.environ.get("STOP_LOSS_DIST", "0"))
TAKE_PROFIT_DIST = float(os.environ.get("TAKE_PROFIT_DIST", "0"))

DEALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capital_deals.json")
SESSION_MAX_AGE = 8 * 60          # re-login well before the token goes stale
RESOLUTION = {"1m": "MINUTE", "5m": "MINUTE_5", "15m": "MINUTE_15", "30m": "MINUTE_30",
              "1h": "HOUR", "4h": "HOUR_4", "1d": "DAY"}


def configured():
    return bool(API_KEY and EMAIL and PASSWORD)


def refresh_keys():
    """Pick up credentials saved from Telegram without restarting the process."""
    global API_KEY, EMAIL, PASSWORD, EPIC, ENGINE_SYMBOL, TRADE_SIZE, DEMO, BASE
    global STOP_LOSS_DIST, TAKE_PROFIT_DIST
    API_KEY = os.environ.get("CAPITAL_API_KEY", "").strip()
    EMAIL = os.environ.get("CAPITAL_EMAIL", "").strip()
    PASSWORD = os.environ.get("CAPITAL_PASSWORD", "").strip()
    EPIC = os.environ.get("CAPITAL_EPIC", "GOLD").strip()
    ENGINE_SYMBOL = os.environ.get("SYMBOL", "GOLD").strip()
    STOP_LOSS_DIST = float(os.environ.get("STOP_LOSS_DIST", "0"))
    TAKE_PROFIT_DIST = float(os.environ.get("TAKE_PROFIT_DIST", "0"))
    TRADE_SIZE = float(os.environ.get("CAPITAL_TRADE_SIZE", "1"))
    DEMO = os.environ.get("CAPITAL_DEMO", "1") not in ("0", "false", "no")
    BASE = ("https://demo-api-capital.backend-capital.com/api/v1" if DEMO
            else "https://api-capital.backend-capital.com/api/v1")


# Some hosts (Oracle Cloud among them) resolve API names to IPv6 addresses they cannot
# actually route, which fails as a connection error rather than anything readable.
# FORCE_IPV4=1 binds outgoing connections to the IPv4 stack.
def _transport():
    if os.environ.get("FORCE_IPV4", "0") in ("1", "true", "yes"):
        return httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return None


def _load_deals():
    try:
        with open(DEALS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_deals(deals):
    try:
        with open(DEALS_PATH, "w") as f:
            json.dump(deals, f)
    except Exception as e:
        log.warning(f"deal map save failed: {e}")


def remember_deal(deal_id, magic):
    deals = _load_deals()
    deals[str(deal_id)] = magic
    _save_deals(deals)


class CapitalBroker:
    """Speaks the same method names as the MetaApi RPC connection the engine uses."""

    def __init__(self):
        self._client = None
        self._cst = None
        self._token = None
        self._logged_in_at = 0.0
        self._epic = EPIC

    # ---------- plumbing ----------

    async def _http(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20, base_url=BASE, transport=_transport())
        return self._client

    async def _login(self):
        client = await self._http()
        r = await client.post("/session",
                              headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
                              json={"identifier": EMAIL, "password": PASSWORD})
        if r.status_code >= 400:
            raise RuntimeError(f"Capital.com login failed ({r.status_code}): {r.text[:200]}")
        self._cst = r.headers.get("CST")
        self._token = r.headers.get("X-SECURITY-TOKEN")
        if not (self._cst and self._token):
            raise RuntimeError("Capital.com login returned no session tokens")
        self._logged_in_at = time.time()
        log.info(f"Capital.com session opened ({'demo' if DEMO else 'live'})")

    async def _headers(self):
        if not self._cst or time.time() - self._logged_in_at > SESSION_MAX_AGE:
            await self._login()
        return {"X-SECURITY-TOKEN": self._token, "CST": self._cst, "Content-Type": "application/json"}

    async def _req(self, method, path, **kw):
        client = await self._http()
        r = await client.request(method, path, headers=await self._headers(), **kw)
        if r.status_code in (401, 403):          # session expired mid-flight: re-login once
            await self._login()
            r = await client.request(method, path, headers=await self._headers(), **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    async def connect(self):
        await self._login()
        await self._resolve_epic()
        return self

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _resolve_epic(self):
        """Confirm the configured instrument exists; otherwise find gold by search."""
        try:
            await self._req("GET", f"/markets/{self._epic}")
            return self._epic
        except Exception as e:
            log.warning(f"epic '{self._epic}' not usable ({e}); searching")
        data = await self._req("GET", "/markets", params={"searchTerm": "gold"})
        for m in data.get("markets", []):
            name = (m.get("instrumentName") or "").lower()
            if "gold" in name and "mini" not in name and m.get("epic"):
                self._epic = m["epic"]
                log.info(f"using epic {self._epic} ({m.get('instrumentName')})")
                return self._epic
        raise RuntimeError("no gold instrument found on this account")

    # ---------- what the engine calls ----------

    async def get_symbol_price(self, symbol=None):
        data = await self._req("GET", f"/markets/{self._epic}")
        snap = data.get("snapshot", {})
        bid, ask = snap.get("bid"), snap.get("offer")
        if bid is None or ask is None:
            raise RuntimeError(f"no quote for {self._epic}")
        return {"bid": float(bid), "ask": float(ask)}

    async def get_account_information(self):
        data = await self._req("GET", "/accounts")
        accounts = data.get("accounts", [])
        acc = next((a for a in accounts if a.get("preferred")), accounts[0] if accounts else {})
        bal = acc.get("balance", {}) or {}
        balance = float(bal.get("balance", 0) or 0)
        pnl = float(bal.get("profitLoss", 0) or 0)
        return {"balance": balance, "equity": round(balance + pnl, 2),
                "currency": acc.get("currency", "USD")}

    async def get_positions(self):
        """Normalized to the shape the engine already expects from MetaApi."""
        data = await self._req("GET", "/positions")
        deals = _load_deals()
        out = []
        for item in data.get("positions", []):
            p = item.get("position", {})
            market = item.get("market", {})
            deal_id = str(p.get("dealId", ""))
            direction = (p.get("direction") or "").upper()
            if market.get("epic", self._epic) != self._epic:
                continue                                  # a position on some other instrument
            out.append({
                "id": deal_id,
                # report the engine's own symbol name: the epic may be spelled differently
                # by the broker, and the engine filters its positions by SYMBOL.
                "symbol": ENGINE_SYMBOL,
                "type": "POSITION_TYPE_BUY" if direction == "BUY" else "POSITION_TYPE_SELL",
                "magic": deals.get(deal_id, 0),          # 0 = not ours, engine ignores it
                "openPrice": float(p.get("level", 0) or 0),
                "volume": float(p.get("size", 0) or 0),
                "unrealizedProfit": float(p.get("upl", p.get("profit", 0)) or 0),
            })
        return out

    async def _open(self, direction, volume, options):
        price = await self.get_symbol_price()
        ref = price["ask"] if direction == "BUY" else price["bid"]
        body = {"epic": self._epic, "direction": direction, "size": float(volume or TRADE_SIZE)}
        if STOP_LOSS_DIST > 0:
            body["stopLevel"] = round(ref - STOP_LOSS_DIST if direction == "BUY" else ref + STOP_LOSS_DIST, 2)
        if TAKE_PROFIT_DIST > 0:
            body["profitLevel"] = round(ref + TAKE_PROFIT_DIST if direction == "BUY" else ref - TAKE_PROFIT_DIST, 2)
        res = await self._req("POST", "/positions", json=body)
        ref_id = res.get("dealReference")
        deal_id = await self._confirm(ref_id) if ref_id else None
        if deal_id:
            remember_deal(deal_id, (options or {}).get("magic", 0))
        return {"dealId": deal_id, "dealReference": ref_id, "stopLevel": body.get("stopLevel")}

    async def _confirm(self, deal_reference):
        """Turn a deal reference into the real dealId, so we can tag and close it later."""
        for _ in range(6):
            try:
                info = await self._req("GET", f"/confirms/{deal_reference}")
                if info.get("dealStatus") in ("ACCEPTED", "SUCCESS", None):
                    affected = info.get("affectedDeals") or []
                    return info.get("dealId") or (affected[0].get("dealId") if affected else None)
                raise RuntimeError(f"order rejected: {info.get('reason') or info.get('dealStatus')}")
            except RuntimeError:
                raise
            except Exception:
                await asyncio.sleep(0.5)
        return None

    async def create_market_buy_order(self, symbol=None, volume=None, options=None):
        return await self._open("BUY", volume, options)

    async def create_market_sell_order(self, symbol=None, volume=None, options=None):
        return await self._open("SELL", volume, options)

    async def close_position(self, position_id):
        res = await self._req("DELETE", f"/positions/{position_id}")
        deals = _load_deals()
        deals.pop(str(position_id), None)
        _save_deals(deals)
        return res

    # ---------- candles (broker-native, so no calibration offset is needed) ----------

    async def get_candles(self, symbol=None, timeframe="1h", limit=200):
        data = await self._req("GET", f"/prices/{self._epic}",
                               params={"resolution": RESOLUTION.get(timeframe, "HOUR"),
                                       "max": min(int(limit), 1000)})
        out = []
        for p in data.get("prices", []):
            def mid(field):
                v = p.get(field) or {}
                bid, ask = v.get("bid"), v.get("ask", v.get("offer"))
                if bid is None and ask is None:
                    return None
                vals = [x for x in (bid, ask) if x is not None]
                return sum(vals) / len(vals)
            o, h, l, c = mid("openPrice"), mid("highPrice"), mid("lowPrice"), mid("closePrice")
            if None in (o, h, l, c):
                continue
            out.append({"time": p.get("snapshotTime"), "open": o, "high": h, "low": l,
                        "close": c, "tickVolume": p.get("lastTradedVolume", 0) or 0})
        return out

    async def dealing_rules(self):
        """Minimum order size and step, so the bot can configure itself instead of guessing."""
        data = await self._req("GET", f"/markets/{self._epic}")
        rules = data.get("dealingRules", {}) or {}

        def val(name, default=None):
            node = rules.get(name) or {}
            v = node.get("value", node if isinstance(node, (int, float)) else None)
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        return {
            "epic": self._epic,
            "name": (data.get("instrument", {}) or {}).get("name", self._epic),
            "min_size": val("minDealSize", 1.0),
            "min_stop_distance": val("minStopOrProfitDistance"),
        }

    # methods the engine calls on a MetaApi connection that have no counterpart here
    async def wait_synchronized(self, **kw):
        return True

    async def subscribe_to_market_data(self, **kw):
        return True


async def _selftest():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"الوضع: {'تجريبي (demo)' if DEMO else 'حقيقي (live)'}")
    if not configured():
        print("❌ ناقص: CAPITAL_API_KEY / CAPITAL_EMAIL / CAPITAL_PASSWORD")
        return
    b = CapitalBroker()
    try:
        await b.connect()
        print(f"✅ الاتصال نجح — الأداة: {b._epic}")
        acc = await b.get_account_information()
        print(f"💰 الرصيد: {acc['balance']} {acc['currency']} | السيولة: {acc['equity']}")
        price = await b.get_symbol_price()
        print(f"🥇 السعر: bid {price['bid']} / ask {price['ask']}")
        candles = await b.get_candles(timeframe="15m", limit=50)
        print(f"📊 الشموع: {len(candles)} شمعة، آخر إغلاق {candles[-1]['close'] if candles else '-'}")
        pos = await b.get_positions()
        print(f"📂 الصفقات المفتوحة: {len(pos)}")
        print("\n🎉 كل شي تمام — تكدر تشغّل البوت على هذا الحساب.")
    except Exception as e:
        print(f"❌ فشل: {e}")
    finally:
        await b.close()


if __name__ == "__main__":
    asyncio.run(_selftest())
