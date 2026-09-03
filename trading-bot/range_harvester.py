import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

from metaapi_cloud_sdk import MetaApi
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest

META_API_TOKEN = os.environ["META_API_TOKEN"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SYMBOL = os.environ.get("SYMBOL", "GOLD")
MAGIC_BUY, MAGIC_SELL = 777111, 777222
TRADE_LOT = float(os.environ.get("TRADE_LOT", "0.03"))
MIN_CASH_PROFIT, PULLBACK_CASH, BREAKOUT_DIST = 2.00, 0.80, 4.50
CONNECTION_STALE_SECS = 25  # if no successful price fetch in this window, force a reconnect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "bot.log")
CHAT_ID_PATH = os.path.join(BASE_DIR, "chat_id.txt")
ENV_PATH = os.path.join(BASE_DIR, ".env")
UPDATE_BRANCH = os.environ.get("UPDATE_BRANCH", "claude/range-harvester-stability-fix-kfz55i")
RAW_BASE = f"https://raw.githubusercontent.com/aloshrahman4-design/alfursan-store/{UPDATE_BRANCH}/trading-bot"
SAVER_STATE_PATH = os.path.join(BASE_DIR, "saver_state.json")
WEEKEND_SAVER = os.environ.get("WEEKEND_SAVER", "1") not in ("0", "false", "no")
WEEKEND_CLOSE_HOUR_UTC = int(os.environ.get("WEEKEND_CLOSE_HOUR_UTC", "21"))  # Friday
WEEKEND_OPEN_HOUR_UTC = int(os.environ.get("WEEKEND_OPEN_HOUR_UTC", "22"))    # Sunday
METAAPI_HOURLY_USD = float(os.environ.get("METAAPI_HOURLY_USD", "0.035"))     # ≈ $5.94 / 168h
ONDEMAND = os.environ.get("ONDEMAND", "1") not in ("0", "false", "no")        # allow idle sleeping at all
DEFAULT_EXEC_MODE = os.environ.get("EXEC_MODE", "fast")                       # fast = instant orders, save = cheapest
SESSION_HOURS_DEFAULT = float(os.environ.get("SESSION_HOURS", "3"))
IDLE_SLEEP_MINUTES = int(os.environ.get("IDLE_SLEEP_MINUTES", "20"))
WAKE_GRACE_MINUTES = int(os.environ.get("WAKE_GRACE_MINUTES", "30"))
WAKE_TIMEOUT_SECS = int(os.environ.get("WAKE_TIMEOUT_SECS", "120"))
SETTABLE_KEYS = {
    "META_API_TOKEN": "توكن MetaApi",
    "ANTHROPIC_API_KEY": "مفتاح Claude",
    "FINNHUB_API_KEY": "مفتاح الأخبار",
    "CLAUDE_MODEL": "نموذج Claude",
    "NEWS_POLL_MINUTES": "دقائق فحص الأخبار",
    "NEWS_MAX_ANALYSES_DAY": "حد تحليلات الأخبار يومياً",
    "DAILY_TRADE_HOUR_UTC": "ساعة صفقة اليوم (UTC)",
    "EXTRA_SYMBOLS": "الأسواق المرتبطة",
}
KEY_ALIASES = {
    "metaapi": "META_API_TOKEN", "token": "META_API_TOKEN", "meta": "META_API_TOKEN",
    "claude": "ANTHROPIC_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "news": "FINNHUB_API_KEY", "finnhub": "FINNHUB_API_KEY",
    "model": "CLAUDE_MODEL", "hour": "DAILY_TRADE_HOUR_UTC",
    "poll": "NEWS_POLL_MINUTES", "symbols": "EXTRA_SYMBOLS",
}
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("harvester")

try:
    import intel  # market intelligence layer (news, multi-timeframe TA, daily trade idea)
except Exception as _e:  # the trading engine must keep running even if intel is broken
    intel = None
    log.warning(f"intel module unavailable: {_e}")

try:
    import datafeed  # free prices, so analysis never keeps a billed account deployed
except Exception as _e:
    datafeed = None
    log.warning(f"datafeed module unavailable: {_e}")

bot = None
api_instance = None
account_instance = None
connection = None
bot_active = True
chat_id_active = None
buy_anchor, sell_anchor = None, None
buy_waiting, sell_waiting = False, False
peak_buy_profit, peak_sell_profit = 0.0, 0.0
total_profit = 0.0
buy_count, sell_count = 0, 0
last_price_ok = 0.0
saver_sleeping = False
slept_at = None
wake_until = 0.0
last_connection_error = ""
exec_mode = DEFAULT_EXEC_MODE   # "fast" = account stays hot, orders are instant; "save" = cheapest
session_until = 0.0             # temporary fast window started from Telegram
analysis_lock = asyncio.Lock()


def load_exec_mode():
    global exec_mode
    exec_mode = load_saver_state().get("mode", DEFAULT_EXEC_MODE)


def load_chat_id():
    global chat_id_active
    env_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env_id:
        chat_id_active = int(env_id)
        return
    try:
        with open(CHAT_ID_PATH) as f:
            chat_id_active = int(f.read().strip())
    except Exception:
        pass


def remember_chat_id(cid):
    global chat_id_active
    if cid and cid != chat_id_active:
        chat_id_active = cid
        try:
            with open(CHAT_ID_PATH, "w") as f:
                f.write(str(cid))
        except Exception as e:
            log.warning(f"could not persist chat id: {e}")


def is_owner(cid):
    """The first chat that ever talked to the bot owns it; later chats are ignored."""
    return chat_id_active is None or cid == chat_id_active


def write_env_value(key, value):
    """Insert or replace KEY=VALUE in .env (systemd EnvironmentFile) and in this process."""
    lines, replaced = [], False
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f.read().splitlines():
                if line.split("=", 1)[0].strip() == key:
                    lines.append(f"{key}={value}")
                    replaced = True
                else:
                    lines.append(line)
    if not replaced:
        lines.append(f"{key}={value}")
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.replace(tmp, ENV_PATH)
    os.chmod(ENV_PATH, 0o600)
    os.environ[key] = value


async def validate_metaapi_token(candidate):
    """Prove a new MetaApi token really opens this account before it replaces the old one."""
    probe = MetaApi(token=candidate)
    try:
        acct = await asyncio.wait_for(probe.metatrader_account_api.get_account(ACCOUNT_ID), timeout=30.0)
        return None, getattr(acct, "state", "?")
    except Exception as e:
        return str(e), None
    finally:
        try:
            await asyncio.wait_for(probe.close(), timeout=5.0)
        except Exception:
            pass


async def handle_setkey(text, chat_id, message_id):
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await send_long("الصيغة: `/setkey claude sk-ant-...`\nأو: `/setkey news <finnhub_key>`")
        return
    name, value = parts[1].strip().lower(), parts[2].strip()
    key = KEY_ALIASES.get(name, name.upper())
    if key not in SETTABLE_KEYS:
        await send_long("مفتاح غير معروف. المسموح: " + "، ".join(sorted(KEY_ALIASES)))
        return
    if not value or "\n" in value:
        await send_long("القيمة غير صالحة.")
        return

    async def forget_message():
        try:  # delete the message so the secret does not linger in the chat history
            await asyncio.wait_for(bot.delete_message(chat_id=chat_id, message_id=message_id), timeout=5.0)
        except Exception as e:
            log.warning(f"could not delete key message: {e}")

    if key == "META_API_TOKEN":
        await send_long("⏳ أفحص التوكن الجديد قبل ما أستبدل القديم...")
        err, state = await validate_metaapi_token(value)
        if err:
            await forget_message()
            await send_long(f"❌ التوكن الجديد ما اشتغل، خليت القديم مثل ما هو.\n`{err[:300]}`", markup=keyboard())
            return
        try:
            shutil.copy2(ENV_PATH, ENV_PATH + ".bak")
        except Exception as e:
            log.warning(f"env backup failed: {e}")
        write_env_value(key, value)
        await forget_message()
        await send_long(
            f"✅ التوكن الجديد صحيح (حالة الحساب: `{state}`) وتم حفظه.\n"
            "♻️ أعيد التشغيل الآن بالتوكن الجديد.\n\n"
            "🔒 بعد ما يرجع البوت ويكتب لك ✅ تم الاتصال، امسح التوكن القديم من لوحة MetaApi.")
        os._exit(0)

    try:
        write_env_value(key, value)
        if intel is not None:
            intel.refresh_keys()
    except Exception as e:
        await send_long(f"⚠️ فشل الحفظ: `{e}`")
        return
    await forget_message()
    status = intel.keys_status() if intel is not None else ""
    await send_long(f"✅ تم حفظ *{SETTABLE_KEYS[key]}* وحذف الرسالة.\n\n{status}", markup=keyboard())


def self_update():
    """Download the newest code, compile it, swap it in. Returns an error string or None."""
    files = ("range_harvester.py", "intel.py", "datafeed.py", "requirements.txt")
    tmpdir = os.path.join(BASE_DIR, ".update")
    os.makedirs(tmpdir, exist_ok=True)
    for name in files:
        got = subprocess.run(["curl", "-fsSL", f"{RAW_BASE}/{name}", "-o", os.path.join(tmpdir, name)],
                             capture_output=True, text=True, timeout=60)
        if got.returncode != 0:
            return f"⚠️ فشل التحميل ({name}): `{got.stderr.strip()[:200]}`"
    py = sys.executable
    pip = subprocess.run([py, "-m", "pip", "install", "-q", "--upgrade", "-r", os.path.join(tmpdir, "requirements.txt")],
                         capture_output=True, text=True, timeout=900)
    if pip.returncode != 0:
        log.warning(f"pip update failed: {pip.stderr[-300:]}")
    comp = subprocess.run([py, "-m", "py_compile", os.path.join(tmpdir, "range_harvester.py"), os.path.join(tmpdir, "intel.py"),
                           os.path.join(tmpdir, "datafeed.py")],
                          capture_output=True, text=True, timeout=120)
    if comp.returncode != 0:
        return f"⚠️ الكود الجديد فيه خطأ، أُلغي التحديث:\n`{comp.stderr.strip()[:400]}`"
    for name in files:
        shutil.copy2(os.path.join(tmpdir, name), os.path.join(BASE_DIR, name))
    return None


HELP_TEXT = (
    "🤖 *أوامر البوت*\n"
    "• /start — لوحة التحكم\n"
    "• `/setkey claude sk-ant-...` — حفظ مفتاح Claude (تُحذف الرسالة تلقائياً)\n"
    "• `/setkey metaapi <token>` — تبديل توكن MetaApi بعد فحصه\n"
    "• /savings — كم وفّرنا من كلفة MetaApi\n"
    "• `/session 3` — جلسة تداول ٣ ساعات بتنفيذ فوري ثم رجوع للتوفير\n"
    "• `/setkey news <key>` — مفتاح Finnhub (اختياري)\n"
    "• /keys — حالة المفاتيح\n"
    "• /update — سحب آخر نسخة من GitHub وإعادة التشغيل\n"
    "• /restart — إعادة تشغيل البوت\n"
    "• /help — هذه القائمة"
)


def keyboard():
    status = "🟢 النظام: شغال" if bot_active else "🔴 النظام: متوقف"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 حالة النطاق والأرباح", callback_data="status"), InlineKeyboardButton("🥇 السعر اللحظي", callback_data="price")],
        [InlineKeyboardButton("🟢 ارتكاز شراء جديد", callback_data="anchor_buy"), InlineKeyboardButton("🔴 ارتكاز بيع جديد", callback_data="anchor_sell")],
        [InlineKeyboardButton("⚔️ ارتكاز مزدوج (شراء + بيع)", callback_data="anchor_dual")],
        [InlineKeyboardButton("🧠 استشارة السوق", callback_data="advisory"), InlineKeyboardButton("📰 آخر الأخبار", callback_data="news")],
        [InlineKeyboardButton("🎯 صفقة اليوم", callback_data="daily_trade")],
        [InlineKeyboardButton(mode_label(), callback_data="toggle_mode")],
        [InlineKeyboardButton(status, callback_data="toggle_bot")],
        [InlineKeyboardButton("🚨 إغلاق جميع صفقات البوت", callback_data="close_all")]
    ])


async def safe_reply(coro_func, **kwargs):
    for attempt in range(2):
        try:
            return await asyncio.wait_for(coro_func(**kwargs), timeout=6.0)
        except Exception as e:
            log.warning(f"reply attempt {attempt+1} failed: {e}")
            await asyncio.sleep(0.4)
    return None


async def notify(msg: str):
    if chat_id_active and bot:
        await send_long(msg)


async def send_long(text: str, markup=None):
    """Send text in <=3900-char chunks; fall back to plain text if Markdown fails."""
    if not (chat_id_active and bot):
        return
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]
    for i, chunk in enumerate(chunks):
        kb = markup if i == len(chunks) - 1 else None
        ok = await safe_reply(bot.send_message, chat_id=chat_id_active, text=chunk, parse_mode="Markdown", reply_markup=kb)
        if ok is None:
            await safe_reply(bot.send_message, chat_id=chat_id_active, text=chunk, reply_markup=kb)


async def fetch_candles(symbol, timeframe, limit=200):
    """Broker candles when the account is already awake, free candles otherwise.
    Analysis must never be a reason to keep a billed account deployed."""
    if account_instance and not saver_sleeping:
        try:
            candles = await asyncio.wait_for(
                account_instance.get_historical_candles(symbol=symbol, timeframe=timeframe, limit=limit), timeout=25.0)
            if candles:
                return candles
        except Exception as e:
            log.warning(f"broker candles failed ({symbol} {timeframe}), using free feed: {e}")
    if datafeed is None:
        raise RuntimeError("no candle source available")
    return await datafeed.get_candles(symbol, timeframe, limit)


def market_closed(now=None):
    """Gold is shut from Friday close to Sunday open. MetaApi bills per deployed hour,
    so those ~49 hours are pure waste unless we undeploy the account."""
    now = now or datetime.now(timezone.utc)
    wd, hour = now.weekday(), now.hour  # Mon=0 … Fri=4, Sat=5, Sun=6
    if wd == 4 and hour >= WEEKEND_CLOSE_HOUR_UTC:
        return True
    if wd == 5:
        return True
    if wd == 6 and hour < WEEKEND_OPEN_HOUR_UTC:
        return True
    return False


def load_saver_state():
    try:
        with open(SAVER_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"saved_hours": 0.0, "since": datetime.now(timezone.utc).strftime("%Y-%m-%d")}


def save_saver_state(state):
    try:
        with open(SAVER_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"saver state save failed: {e}")


def savings_line():
    st = load_saver_state()
    h = st.get("saved_hours", 0.0)
    line = f"💰 وفّرنا `{round(h)}` ساعة تشغيل MetaApi ≈ `{round(h * METAAPI_HOURLY_USD, 2)}$` منذ {st.get('since','')}"
    since = st.get("since", "")
    try:
        days = max((datetime.now(timezone.utc) - datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days, 1)
        billed = max(days * 24 - h, 0)
        line += (f"\n📉 الكلفة الفعلية ≈ `{round(billed * METAAPI_HOURLY_USD, 2)}$` بدل "
                 f"`{round(days * 24 * METAAPI_HOURLY_USD, 2)}$` — أي `{round(h / (days * 24) * 100)}%` توفير")
    except Exception:
        pass
    return line


async def our_positions_open():
    if not connection:
        return False
    try:
        pos = await asyncio.wait_for(connection.get_positions(), timeout=6.0)
        return any(p.get("symbol") == SYMBOL and p.get("magic") in (MAGIC_BUY, MAGIC_SELL) for p in pos)
    except Exception as e:
        log.warning(f"position check before sleeping failed: {e}")
        return True  # unknown state -> stay awake, never risk an unwatched position


def fast_now():
    """True while orders must be instant: fast mode, or a trading session the user opened."""
    return exec_mode == "fast" or time.time() < session_until


def mode_label():
    if time.time() < session_until:
        mins = int((session_until - time.time()) / 60)
        return f"⚡ جلسة تداول ({mins} دقيقة متبقية)"
    return "⚡ التنفيذ: فوري" if exec_mode == "fast" else "💤 التنفيذ: اقتصادي"


def set_exec_mode(mode):
    global exec_mode
    exec_mode = mode
    st = load_saver_state()
    st["mode"] = mode
    save_saver_state(st)


async def work_pending():
    """Is there anything that genuinely requires a live broker connection?
    An armed anchor or an open position: yes. Idle watching: no — free data covers that."""
    if buy_anchor is not None or sell_anchor is not None:
        return True
    return await our_positions_open()


async def wake_for_execution(reason="أمر تنفيذ"):
    """Bring the billed account back up on demand, and wait until it can actually trade."""
    global saver_sleeping, wake_until
    wake_until = time.time() + WAKE_GRACE_MINUTES * 60
    if connection:
        return True
    saver_sleeping = False
    await notify(f"⚡ *{reason}:* أشغّل حساب MetaApi الآن...")
    for _ in range(int(WAKE_TIMEOUT_SECS / 2)):
        if connection:
            return True
        await asyncio.sleep(2)
    hint = billing_hint(last_connection_error) if last_connection_error else None
    await notify(hint or "⚠️ ما قدرت أشغّل الحساب خلال الوقت المتوقع. جرب مرة ثانية بعد لحظات.")
    return False


async def go_to_sleep(message):
    global saver_sleeping, slept_at
    acct = account_instance
    if not acct:
        return False
    await teardown_connection()
    try:
        await asyncio.wait_for(acct.undeploy(), timeout=60.0)
    except Exception as e:
        log.warning(f"undeploy failed: {e}")
        return False
    saver_sleeping = True
    slept_at = time.time()
    await notify(message)
    return True


async def wake_up(message):
    global saver_sleeping, slept_at
    saver_sleeping = False  # ensure_connection redeploys on its next pass
    if slept_at:
        st = load_saver_state()
        st["saved_hours"] = round(st.get("saved_hours", 0.0) + (time.time() - slept_at) / 3600, 2)
        save_saver_state(st)
        slept_at = None
    if message:
        await notify(message)


async def cost_governor():
    """Keep the MetaApi account deployed only while it is actually needed.

    Deployed hours are the whole MetaApi bill, and the account is only needed to send
    and watch orders. So: asleep on weekends, asleep while idle, awake the moment an
    anchor is armed, a position is open, or a button asks to trade."""
    global saver_sleeping
    if not WEEKEND_SAVER:
        log.info("cost governor disabled")
        return
    warned_positions = False
    idle_since = None
    last_calibration = 0.0
    while True:
        try:
            closed = market_closed()
            needed = await work_pending() or time.time() < wake_until

            if not saver_sleeping and account_instance:
                if closed and not await our_positions_open():
                    idle_since = None
                    await go_to_sleep("😴 *توفير العطلة:* السوق مغلق، أوقفت حساب MetaApi.\n"
                                      "راح أرجّعه تلقائياً قبل الفتح.")
                elif closed and not warned_positions:
                    warned_positions = True
                    await notify("⚠️ السوق سكّر وعندك صفقة مفتوحة، فبقيت شغّال. سكّرها إذا تريد توفير الكلفة.")
                elif not closed and ONDEMAND and not fast_now() and not needed:
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since >= IDLE_SLEEP_MINUTES * 60:
                        idle_since = None
                        await go_to_sleep(
                            f"💤 *توفير الخمول:* ماكو ارتكاز مسلّح ولا صفقة مفتوحة منذ {IDLE_SLEEP_MINUTES} دقيقة، "
                            "فأوقفت حساب MetaApi.\nالتحليل والأخبار مستمرة بالمصدر المجاني، وأي زر تداول يرجّعه بثواني.")
                else:
                    idle_since = None
                    warned_positions = False

            elif saver_sleeping and (needed or (not closed and (fast_now() or not ONDEMAND))):
                await wake_up("🌅 *رجعت أشتغل:* الحساب يشتغل والاتصال جارٍ.\n" + savings_line())

            # While awake, learn the gap between the broker's quote and the free feed,
            # so levels computed from free data stay in the broker's price frame.
            if connection and datafeed and time.time() - last_calibration > 1800:
                last_calibration = time.time()
                try:
                    p = await asyncio.wait_for(connection.get_symbol_price(symbol=SYMBOL), timeout=5.0)
                    mid = (p.get("bid", 0) + p.get("ask", 0)) / 2
                    free = await datafeed.get_candles(SYMBOL, "15m", 40, apply_offset=False)
                    datafeed.note_broker_price(SYMBOL, mid, free[-1]["close"])
                except Exception as e:
                    log.info(f"calibration skipped: {e}")
        except Exception as e:
            log.warning(f"cost_governor error: {e}")
        await asyncio.sleep(30)


def billing_hint(err_text):
    """Turn an opaque MetaApi failure into the one sentence that tells the user what to do."""
    t = err_text.lower()
    # "please top up your account" is what MetaApi actually returns when the balance runs out.
    if any(k in t for k in ("top up", "top-up", "topup", "payment", "402", "insufficient",
                            "balance", "subscription", "billing", "expired", "quota")):
        return ("💳 *مشكلة اشتراك/رصيد MetaApi.* الحساب موقوف من طرفهم.\n"
                "افتح app.metaapi.cloud → Billing واشحن الرصيد أو فعّل الشحن التلقائي (Auto top-up)، "
                "وراح يرجع البوت لحاله بدون أي أمر منك.")
    if any(k in t for k in ("unauthorized", "401", "invalid token", "forbidden", "403")):
        return ("🔑 *التوكن مرفوض.* أرسل لي التوكن الجديد بالأمر:\n"
                "`/setkey metaapi <التوكن>`\nراح أفحصه وأشغّل نفسي عليه.")
    return None


async def teardown_connection():
    global connection, account_instance, api_instance
    old_conn = connection
    connection = None
    if old_conn:
        try:
            await asyncio.wait_for(old_conn.close(), timeout=5.0)
        except Exception as e:
            log.warning(f"error closing stale connection: {e}")
    account_instance = None
    api_instance = None


async def ensure_connection():
    global connection, account_instance, api_instance, last_price_ok
    fail_streak, hinted = 0, None
    while True:
        try:
            if saver_sleeping:
                await asyncio.sleep(5)
                continue
            if connection:
                if last_price_ok and (time.time() - last_price_ok) > CONNECTION_STALE_SECS:
                    log.warning("No successful price fetch recently -> connection considered stale, forcing reconnect")
                    await teardown_connection()
                    continue
                await asyncio.sleep(3)
                continue

            log.info("Connecting MetaApi...")
            api_instance = MetaApi(token=META_API_TOKEN)
            account_instance = await api_instance.metatrader_account_api.get_account(ACCOUNT_ID)
            if account_instance.state != "DEPLOYED":
                await account_instance.deploy()
            await account_instance.wait_connected(timeout_in_seconds=30)
            conn = account_instance.get_rpc_connection()
            await conn.connect()
            await conn.wait_synchronized(timeout_in_seconds=30)
            try:
                await conn.subscribe_to_market_data(symbol=SYMBOL)
            except Exception:
                pass
            connection = conn
            last_price_ok = time.time()
            fail_streak, hinted = 0, None
            log.info("Connected MetaApi OK!")
            await notify("✅ **تم الاتصال بـ MetaApi بنجاح.**")
        except Exception as e:
            global last_connection_error
            log.error(f"Connection error: {e}", exc_info=True)
            last_connection_error = str(e)
            fail_streak += 1
            hint = billing_hint(str(e))
            # Say it once, only after retries have clearly failed: this is a wall, not a blip.
            if hint and fail_streak >= 3 and hint != hinted:
                hinted = hint
                await notify(hint)
            await teardown_connection()
            await asyncio.sleep(min(5 * fail_streak, 60))


async def safe_close_position(pos_id: str):
    for attempt in range(3):
        try:
            await asyncio.wait_for(connection.close_position(pos_id), timeout=5.0)
            return True
        except Exception as e:
            log.warning(f"close_position attempt {attempt+1} failed: {e}")
            await asyncio.sleep(0.5)
    return False


async def set_new_buy_anchor(manual=False):
    global buy_anchor, buy_waiting, peak_buy_profit
    if not connection:
        return
    try:
        p = await asyncio.wait_for(connection.get_symbol_price(symbol=SYMBOL), timeout=3.0)
        ask = p.get("ask", 0)
        if ask == 0:
            return
        buy_anchor, buy_waiting, peak_buy_profit = ask, False, 0.0
        await asyncio.wait_for(connection.create_market_buy_order(symbol=SYMBOL, volume=TRADE_LOT, options={"magic": MAGIC_BUY}), timeout=5.0)
        src = "يدويا 🎯" if manual else "تلقائيا 🚀"
        await notify(f"🟢 **ارتكاز شراء جديد ({src}):**\n• السعر: `{buy_anchor}`")
    except Exception as e:
        log.error(f"Buy err: {e}", exc_info=True)


async def set_new_sell_anchor(manual=False):
    global sell_anchor, sell_waiting, peak_sell_profit
    if not connection:
        return
    try:
        p = await asyncio.wait_for(connection.get_symbol_price(symbol=SYMBOL), timeout=3.0)
        bid = p.get("bid", 0)
        if bid == 0:
            return
        sell_anchor, sell_waiting, peak_sell_profit = bid, False, 0.0
        await asyncio.wait_for(connection.create_market_sell_order(symbol=SYMBOL, volume=TRADE_LOT, options={"magic": MAGIC_SELL}), timeout=5.0)
        src = "يدويا 🎯" if manual else "تلقائيا 📉"
        await notify(f"🔴 **ارتكاز بيع جديد ({src}):**\n• السعر: `{sell_anchor}`")
    except Exception as e:
        log.error(f"Sell err: {e}", exc_info=True)


async def range_engine():
    global bot_active, connection, buy_anchor, sell_anchor, buy_waiting, sell_waiting
    global peak_buy_profit, peak_sell_profit, total_profit, buy_count, sell_count, last_price_ok

    while True:
        try:
            if not (bot_active and connection):
                await asyncio.sleep(1.0)
                continue
            price_data = await asyncio.wait_for(connection.get_symbol_price(symbol=SYMBOL), timeout=4.0)
            bid, ask = price_data.get("bid", 0), price_data.get("ask", 0)
            if ask == 0 or bid == 0:
                await asyncio.sleep(0.3)
                continue
            last_price_ok = time.time()

            all_pos = await asyncio.wait_for(connection.get_positions(), timeout=4.0)
            buy_pos = [p for p in all_pos if p.get("symbol") == SYMBOL and p.get("type") == "POSITION_TYPE_BUY" and p.get("magic") == MAGIC_BUY]
            sell_pos = [p for p in all_pos if p.get("symbol") == SYMBOL and p.get("type") == "POSITION_TYPE_SELL" and p.get("magic") == MAGIC_SELL]

            if len(buy_pos) > 0:
                b = buy_pos[-1]
                if buy_anchor is None:
                    buy_anchor = b.get("openPrice", ask)
                curr_profit = float(b.get("unrealizedProfit", 0.0))
                if curr_profit >= MIN_CASH_PROFIT:
                    if curr_profit > peak_buy_profit:
                        peak_buy_profit = curr_profit
                    if curr_profit <= (peak_buy_profit - PULLBACK_CASH):
                        if await safe_close_position(b["id"]):
                            total_profit += curr_profit
                            buy_count += 1
                            buy_waiting = True
                            captured = peak_buy_profit
                            peak_buy_profit = 0.0
                            await notify(f"🟢 **حصد قمة الشراء!** القمة: `+{round(captured,2)}$` | الإغلاق: `+{round(curr_profit,2)}$`")
                            await asyncio.sleep(1.5)
            elif buy_anchor is not None and buy_waiting:
                if ask >= (buy_anchor + BREAKOUT_DIST):
                    await set_new_buy_anchor(manual=False)
                    await asyncio.sleep(2.0)
                elif ask <= (buy_anchor + 0.25):
                    buy_waiting, peak_buy_profit = False, 0.0
                    await asyncio.wait_for(connection.create_market_buy_order(symbol=SYMBOL, volume=TRADE_LOT, options={"magic": MAGIC_BUY}), timeout=5.0)
                    await notify(f"🟢 **فتح شراء جديد لعودة السعر لارتكاز (`{buy_anchor}`)**")
                    await asyncio.sleep(2.0)

            if len(sell_pos) > 0:
                s = sell_pos[-1]
                if sell_anchor is None:
                    sell_anchor = s.get("openPrice", bid)
                curr_profit = float(s.get("unrealizedProfit", 0.0))
                if curr_profit >= MIN_CASH_PROFIT:
                    if curr_profit > peak_sell_profit:
                        peak_sell_profit = curr_profit
                    if curr_profit <= (peak_sell_profit - PULLBACK_CASH):
                        if await safe_close_position(s["id"]):
                            total_profit += curr_profit
                            sell_count += 1
                            sell_waiting = True
                            captured = peak_sell_profit
                            peak_sell_profit = 0.0
                            await notify(f"🔴 **حصد قاع البيع!** القمة: `+{round(captured,2)}$` | الإغلاق: `+{round(curr_profit,2)}$`")
                            await asyncio.sleep(1.5)
            elif sell_anchor is not None and sell_waiting:
                if bid <= (sell_anchor - BREAKOUT_DIST):
                    await set_new_sell_anchor(manual=False)
                    await asyncio.sleep(2.0)
                elif bid >= (sell_anchor - 0.25):
                    sell_waiting, peak_sell_profit = False, 0.0
                    await asyncio.wait_for(connection.create_market_sell_order(symbol=SYMBOL, volume=TRADE_LOT, options={"magic": MAGIC_SELL}), timeout=5.0)
                    await notify(f"🔴 **فتح بيع جديد لعودة السعر لارتكاز (`{sell_anchor}`)**")
                    await asyncio.sleep(2.0)
        except Exception as e:
            log.warning(f"Engine iteration error: {e}", exc_info=True)
            await asyncio.sleep(1.0)
        await asyncio.sleep(0.4)


async def run_analysis(query, label, producer):
    """Long-running Claude/news analyses: acknowledge fast, deliver as a new message."""
    if intel is None:
        await safe_reply(query.edit_message_text, text="⚠️ وحدة التحليل غير مثبتة على السيرفر.", reply_markup=keyboard(), parse_mode="Markdown")
        return
    if not account_instance and datafeed is None:
        await safe_reply(query.edit_message_text, text="⚠️ لا يوجد مصدر أسعار متاح.", reply_markup=keyboard(), parse_mode="Markdown")
        return
    if analysis_lock.locked():
        await safe_reply(query.edit_message_text, text="⏳ تحليل آخر قيد التنفيذ، انتظر لحظة.", reply_markup=keyboard(), parse_mode="Markdown")
        return
    async with analysis_lock:
        await safe_reply(query.edit_message_text, text=f"⏳ {label}... (قد يستغرق حتى دقيقة)", reply_markup=keyboard(), parse_mode="Markdown")
        try:
            text = await asyncio.wait_for(producer(), timeout=180.0)
        except Exception as e:
            log.error(f"{label} failed: {e}", exc_info=True)
            text = f"⚠️ فشل {label}: `{e}`"
        await send_long(text, markup=keyboard())


async def handle_callback(query):
    global bot_active, connection
    try:
        if not is_owner(query.message.chat_id):
            log.warning("ignoring callback from a non-owner chat")
            return
        remember_chat_id(query.message.chat_id)
        await asyncio.wait_for(query.answer(), timeout=2.0)
        data = query.data
        log.info(f"Telegram action: {data}")

        if data == "toggle_mode":
            global session_until
            session_until = 0.0
            set_exec_mode("save" if exec_mode == "fast" else "fast")
            if exec_mode == "fast":
                txt = ("⚡ **وضع التنفيذ الفوري**\nالحساب يبقى جاهز طول ساعات السوق، فالارتكاز ينفتح فوراً "
                       "بدون انتظار.\nالكلفة أعلى — استخدم الاقتصادي لما ما تكون متابع.")
                asyncio.create_task(wake_for_execution("تشغيل الوضع الفوري"))
            else:
                txt = ("💤 **وضع التوفير**\nالحساب ينام وقت الخمول، وأول ارتكاز بعد النوم ياخذ نص دقيقة تقريباً "
                       "لين يجهز.\nالتحليل والأخبار يبقون شغالين مجاناً.")
            await safe_reply(query.edit_message_text, text=txt, reply_markup=keyboard(), parse_mode="Markdown")
        elif data == "toggle_bot":
            bot_active = not bot_active
            txt = "تم **تشغيل النظام** 🟢" if bot_active else "تم **إيقاف النظام** 🔴"
            await safe_reply(query.edit_message_text, text=txt, reply_markup=keyboard(), parse_mode="Markdown")
        elif data in ("anchor_buy", "anchor_sell", "anchor_dual"):
            if not await wake_for_execution("طلب ارتكاز"):
                return
            if data in ("anchor_buy", "anchor_dual"):
                await set_new_buy_anchor(manual=True)
            if data == "anchor_dual":
                await asyncio.sleep(0.5)
            if data in ("anchor_sell", "anchor_dual"):
                await set_new_sell_anchor(manual=True)
        elif data == "status":
            if connection:
                try:
                    info = await asyncio.wait_for(connection.get_account_information(), timeout=4.0)
                    all_pos = await asyncio.wait_for(connection.get_positions(), timeout=4.0)
                    b_pos = [p for p in all_pos if p.get("symbol") == SYMBOL and p.get("type") == "POSITION_TYPE_BUY" and p.get("magic") == MAGIC_BUY]
                    s_pos = [p for p in all_pos if p.get("symbol") == SYMBOL and p.get("type") == "POSITION_TYPE_SELL" and p.get("magic") == MAGIC_SELL]
                    b_prof = b_pos[-1].get("unrealizedProfit", 0) if b_pos else 0
                    s_prof = s_pos[-1].get("unrealizedProfit", 0) if s_pos else 0
                    b_txt = f"مفتوحة ({round(b_prof,2)}$)" if b_pos else ("انتظار" if buy_waiting else "جاهز")
                    s_txt = f"مفتوحة ({round(s_prof,2)}$)" if s_pos else ("انتظار" if sell_waiting else "جاهز")
                    bal, curr, eq = info.get("balance"), info.get("currency"), info.get("equity")
                    txt = (f"📊 **الحالة:**\n• الرصيد: `{bal} {curr}` | السيولة: `{eq}`\n"
                           f"• شراء: `{buy_anchor}` ({b_txt})\n• بيع: `{sell_anchor}` ({s_txt})\n"
                           f"• الأرباح: `{round(total_profit,2)}$`\n{savings_line()}")
                except Exception as e:
                    txt = f"⚠️ خطأ: `{e}`"
            elif saver_sleeping:
                txt = f"😴 **وضع توفير العطلة** — السوق مغلق وحساب MetaApi موقوف مؤقتاً.\n{savings_line()}"
            else:
                txt = "⚠️ جاري الاتصال..."
            await safe_reply(query.edit_message_text, text=txt, reply_markup=keyboard(), parse_mode="Markdown")
        elif data == "price":
            if connection:
                try:
                    p = await asyncio.wait_for(connection.get_symbol_price(symbol=SYMBOL), timeout=3.0)
                    txt = f"🥇 **الذهب:** Ask: `{p.get('ask')}` | Bid: `{p.get('bid')}`"
                except Exception:
                    txt = "🥇 جاري السعر..."
            elif datafeed:
                try:
                    p = await datafeed.get_price(SYMBOL)
                    txt = (f"🥇 **الذهب (تقريبي):** Ask: `{p['ask']}` | Bid: `{p['bid']}`\n"
                           f"{datafeed.calibration_line()}\nالحساب المدفوع نائم — اضغط ارتكاز إذا تريد تنفيذ.")
                except Exception as e:
                    txt = f"⚠️ تعذّر جلب السعر المجاني: `{e}`"
            else:
                txt = "⚠️ جاري الاتصال..."
            await safe_reply(query.edit_message_text, text=txt, reply_markup=keyboard(), parse_mode="Markdown")
        elif data == "advisory":
            await run_analysis(query, "تحليل السوق", lambda: intel.market_brief_message(fetch_candles, SYMBOL))
        elif data == "news":
            await run_analysis(query, "جلب الأخبار", lambda: intel.news_now_message(fetch_candles, SYMBOL))
        elif data == "daily_trade":
            await run_analysis(query, "إعداد صفقة اليوم", lambda: intel.daily_trade_message(fetch_candles, SYMBOL))
        elif data == "close_all":
            if not connection and saver_sleeping:
                await wake_for_execution("طلب إغلاق الصفقات")
            if connection:
                try:
                    all_pos = await asyncio.wait_for(connection.get_positions(), timeout=4.0)
                    for p in all_pos:
                        if p.get("symbol") == SYMBOL and p.get("magic") in (MAGIC_BUY, MAGIC_SELL):
                            try:
                                await asyncio.wait_for(connection.close_position(p["id"]), timeout=5.0)
                            except Exception as e:
                                log.warning(f"failed closing {p.get('id')}: {e}")
                    txt = "🚨 **تم إغلاق صفقات الذهب.**"
                except Exception as e:
                    txt = f"⚠️ `{e}`"
                await safe_reply(query.edit_message_text, text=txt, reply_markup=keyboard(), parse_mode="Markdown")
    except Exception as e:
        log.error(f"Callback error: {e}", exc_info=True)


async def process_update(update):
    try:
        if update.message and update.message.text:
            cid = update.message.chat_id
            if not is_owner(cid):
                log.warning(f"ignoring message from non-owner chat {cid}")
                return
            remember_chat_id(cid)
            text = update.message.text.strip()
            if text.startswith("/start"):
                await safe_reply(bot.send_message, chat_id=chat_id_active, text="🎯 **لوحة تحكم Range Harvester:**", reply_markup=keyboard(), parse_mode="Markdown")
            elif text.startswith("/setkey"):
                await handle_setkey(text, cid, update.message.message_id)
            elif text.startswith("/session"):
                global session_until
                parts = text.split()
                try:
                    hours = float(parts[1]) if len(parts) > 1 else SESSION_HOURS_DEFAULT
                except ValueError:
                    hours = SESSION_HOURS_DEFAULT
                hours = max(0.25, min(hours, 24))
                session_until = time.time() + hours * 3600
                await send_long(f"⚡ فتحت جلسة تداول لمدة `{hours}` ساعة — التنفيذ فوري خلالها، "
                                "وبعدها يرجع للتوفير تلقائياً.", markup=keyboard())
                asyncio.create_task(wake_for_execution("بداية جلسة التداول"))
            elif text.startswith("/savings"):
                state = "😴 نائم الآن (توفير)" if saver_sleeping else ("🟢 السوق مفتوح" if not market_closed() else "⏳ بانتظار إغلاق الصفقات")
                await send_long(f"{savings_line()}\n• الحالة: {state}\n• الوضع: {mode_label()}", markup=keyboard())
            elif text.startswith("/keys"):
                await send_long(intel.keys_status() if intel is not None else "⚠️ وحدة التحليل غير مثبتة.", markup=keyboard())
            elif text.startswith("/update"):
                await send_long("⏳ جاري سحب آخر نسخة من GitHub...")
                err = await asyncio.to_thread(self_update)
                if err:
                    await send_long(err, markup=keyboard())
                else:
                    await send_long("✅ تم التحديث. جاري إعادة التشغيل...")
                    os._exit(0)
            elif text.startswith("/restart"):
                await send_long("♻️ إعادة التشغيل...")
                os._exit(0)
            elif text.startswith("/help"):
                await send_long(HELP_TEXT, markup=keyboard())
        elif update.callback_query:
            asyncio.create_task(handle_callback(update.callback_query))
    except Exception as e:
        log.error(f"Process update error: {e}", exc_info=True)


async def telegram_listener():
    offset = None
    while True:
        try:
            updates = await asyncio.wait_for(bot.get_updates(offset=offset, timeout=10), timeout=20)
            for u in updates:
                offset = u.update_id + 1
                asyncio.create_task(process_update(u))
        except Exception as e:
            log.warning(f"telegram poll error: {e}")
            await asyncio.sleep(2)


async def supervise(name, coro_func):
    """Restart a task loop if it ever raises instead of letting the whole process die silently."""
    while True:
        try:
            await coro_func()
            log.warning(f"Task '{name}' exited; not restarting")
            return
        except Exception as e:
            log.error(f"Task '{name}' crashed unexpectedly: {e}", exc_info=True)
            await notify(f"⚠️ **خطأ داخلي في '{name}'، جاري إعادة التشغيل تلقائياً...**\n`{e}`")
            await asyncio.sleep(3)


async def main():
    global bot
    load_chat_id()
    load_exec_mode()
    request = HTTPXRequest(connection_pool_size=8, connect_timeout=5.0, read_timeout=15.0, write_timeout=10.0, pool_timeout=5.0)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)
    await bot.initialize()
    try:
        await bot.set_my_commands([
            BotCommand("start", "لوحة التحكم"),
            BotCommand("keys", "حالة المفاتيح"),
            BotCommand("savings", "كم وفّرنا من كلفة MetaApi"),
            BotCommand("session", "جلسة تداول بتنفيذ فوري: /session 3"),
            BotCommand("setkey", "حفظ مفتاح: /setkey claude sk-ant-..."),
            BotCommand("update", "تحديث الكود من GitHub"),
            BotCommand("restart", "إعادة تشغيل البوت"),
            BotCommand("help", "قائمة الأوامر"),
        ])
    except Exception as e:
        log.warning(f"set_my_commands failed: {e}")
    log.info("Bot booting up (supervised mode)")
    tasks = [
        supervise("connection", ensure_connection),
        supervise("range_engine", range_engine),
        supervise("telegram_listener", telegram_listener),
        supervise("cost_governor", cost_governor),
    ]
    if intel is not None:
        tasks.append(supervise("news_watcher", lambda: intel.news_watcher(fetch_candles, SYMBOL, notify)))
        tasks.append(supervise("daily_scheduler", lambda: intel.daily_scheduler(fetch_candles, SYMBOL, notify)))
        log.info(f"intel enabled: claude={'yes' if intel.claude_ready() else 'no'} news={'yes' if intel.FINNHUB_API_KEY else 'no'}")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
