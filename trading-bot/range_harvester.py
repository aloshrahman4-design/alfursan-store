import asyncio
import hmac
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

META_API_TOKEN = os.environ.get("META_API_TOKEN", "")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SYMBOL = os.environ.get("SYMBOL", "GOLD")
MAGIC_BUY, MAGIC_SELL = 777111, 777222
TRADE_LOT = float(os.environ.get("TRADE_LOT", "0.03"))
# The harvest thresholds are money, but the strategy is really about price distance:
# +2.00$ on 0.03 lot is a ~6.7$ move in gold. Shrink the lot without shrinking these and
# the same 2.00$ would need a 20$ move, so the bot would almost never harvest. Scale them
# with the lot instead, and the behaviour in price terms stays identical.
REF_LOT = 0.03
_scale = TRADE_LOT / REF_LOT if REF_LOT else 1.0
MIN_CASH_PROFIT = round(float(os.environ.get("MIN_CASH_PROFIT", 2.00 * _scale)), 2)
PULLBACK_CASH = round(float(os.environ.get("PULLBACK_CASH", 0.80 * _scale)), 2)
# Give-back as a share of the peak profit rather than a fixed sum: a trade that ran to +40$
# should be allowed to breathe more than one sitting at +6$. Set to 0 to use PULLBACK_CASH.
PULLBACK_PCT = float(os.environ.get("PULLBACK_PCT", "10"))
BREAKOUT_DIST = float(os.environ.get("BREAKOUT_DIST", "4.50"))   # a price distance: never scaled
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
BROKER = os.environ.get("BROKER", "metaapi").strip().lower()   # metaapi | capital | paper
LEAN = os.environ.get("LEAN", "0") in ("1", "true", "yes")   # trading only: no news, no analysis
SIGNAL = os.environ.get("SIGNAL", "0") in ("1", "true", "yes")  # bot watches and alerts, user taps in MT5
ONDEMAND = os.environ.get("ONDEMAND", "1") not in ("0", "false", "no")        # allow idle sleeping at all
DEFAULT_EXEC_MODE = os.environ.get("EXEC_MODE", "fast")                       # fast = instant orders, save = cheapest
SESSION_HOURS_DEFAULT = float(os.environ.get("SESSION_HOURS", "3"))
IDLE_SLEEP_MINUTES = int(os.environ.get("IDLE_SLEEP_MINUTES", "20"))
WAKE_GRACE_MINUTES = int(os.environ.get("WAKE_GRACE_MINUTES", "30"))
WAKE_TIMEOUT_SECS = int(os.environ.get("WAKE_TIMEOUT_SECS", "120"))
# A never-deployed account has to be provisioned on MetaApi's side and then log in to the
# broker before it can quote anything. That is minutes on the first run, so a 30s wait made
# a perfectly healthy new account report "Timed out waiting for MetaApi to synchronize".
DEPLOY_TIMEOUT_SECS = int(os.environ.get("DEPLOY_TIMEOUT_SECS", "600"))
# --- TradingView webhook ------------------------------------------------------------
# TradingView posts alerts to ports 80/443 only, over IPv4, and cancels a request that
# takes more than three seconds. It sends no headers we could authenticate with, so the
# secret lives in the URL path and the four addresses it publishes are the only senders
# accepted. HOOK_MODE starts at "watch": signals are reported, not traded, until the user
# has seen them arrive correctly and arms it with /hook trade.
HOOK_ENABLED = os.environ.get("HOOK_ENABLED", "1") not in ("0", "false", "no")
HOOK_PORT = int(os.environ.get("HOOK_PORT", "80"))
HOOK_SECRET = os.environ.get("HOOK_SECRET", "")
HOOK_MODE = os.environ.get("HOOK_MODE", "watch").strip().lower()      # watch | trade
HOOK_HOST = os.environ.get("HOOK_HOST", "").strip()
HOOK_ALLOW_IPS = os.environ.get(
    "HOOK_ALLOW_IPS", "52.89.214.238,34.212.75.30,54.218.53.128,52.32.178.7")
HOOK_MAX_BODY = 8192
SETTABLE_KEYS = {
    "META_API_TOKEN": "توكن MetaApi",
    "ACCOUNT_ID": "رقم الحساب في MetaApi",
    "SYMBOL": "اسم أداة الذهب عند الوسيط",
    "ANTHROPIC_API_KEY": "مفتاح Claude",
    "FINNHUB_API_KEY": "مفتاح الأخبار",
    "CLAUDE_MODEL": "نموذج Claude",
    "NEWS_POLL_MINUTES": "دقائق فحص الأخبار",
    "NEWS_MAX_ANALYSES_DAY": "حد تحليلات الأخبار يومياً",
    "DAILY_TRADE_HOUR_UTC": "ساعة صفقة اليوم (UTC)",
    "EXTRA_SYMBOLS": "الأسواق المرتبطة",
    "BROKER": "منصة التنفيذ (metaapi أو capital أو paper)",
    "CAPITAL_API_KEY": "مفتاح Capital.com",
    "CAPITAL_EMAIL": "إيميل Capital.com",
    "CAPITAL_PASSWORD": "كلمة سر Capital.com",
    "CAPITAL_DEMO": "حساب تجريبي (1) أو حقيقي (0)",
    "CAPITAL_TRADE_SIZE": "حجم الصفقة عند Capital.com",
    "FORCE_IPV4": "إجبار الاتصال على IPv4 (1 أو 0)",
    "LEAN": "وضع التداول فقط بدون أخبار وتحليل (1 أو 0)",
    "SIGNAL": "وضع الإشارات: البوت ينبّه وأنت تنفّذ (1 أو 0)",
    "TRADE_LOT": "حجم اللوت",
    "MIN_CASH_PROFIT": "حد الحصاد بالدولار",
    "PULLBACK_CASH": "الارتداد المسموح من القمة بالدولار",
    "PULLBACK_PCT": "الارتداد المسموح كنسبة % من القمة (0 = استخدم الدولار)",
    "BREAKOUT_DIST": "مسافة الاختراق للارتكاز الجديد",
    "HOOK_ENABLED": "استقبال إشارات TradingView (1 أو 0)",
    "HOOK_MODE": "تصرف الإشارة: watch مراقبة فقط أو trade تنفيذ",
    "HOOK_PORT": "منفذ استقبال الإشارات (80 أو 443)",
    "HOOK_HOST": "عنوان السيرفر العام للرابط",
    "HOOK_SECRET": "السر داخل رابط الإشارات",
    "HOOK_ALLOW_IPS": "عناوين TradingView المسموحة (فاضي = الكل)",
    "STOP_LOSS_DIST": "مسافة وقف الخسارة بالدولار",
    "TAKE_PROFIT_DIST": "مسافة الهدف بالدولار",
}
KEY_ALIASES = {
    "metaapi": "META_API_TOKEN", "token": "META_API_TOKEN", "meta": "META_API_TOKEN",
    "account": "ACCOUNT_ID", "acc": "ACCOUNT_ID", "id": "ACCOUNT_ID",
    "gold": "SYMBOL", "symbol": "SYMBOL", "asset": "SYMBOL",
    "claude": "ANTHROPIC_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "news": "FINNHUB_API_KEY", "finnhub": "FINNHUB_API_KEY",
    "model": "CLAUDE_MODEL", "hour": "DAILY_TRADE_HOUR_UTC",
    "poll": "NEWS_POLL_MINUTES", "symbols": "EXTRA_SYMBOLS",
    "broker": "BROKER", "capkey": "CAPITAL_API_KEY", "capmail": "CAPITAL_EMAIL",
    "cappass": "CAPITAL_PASSWORD", "capdemo": "CAPITAL_DEMO", "capsize": "CAPITAL_TRADE_SIZE",
    "sl": "STOP_LOSS_DIST", "tp": "TAKE_PROFIT_DIST", "ipv4": "FORCE_IPV4", "lean": "LEAN", "signal": "SIGNAL", "lot": "TRADE_LOT",
    "profit": "MIN_CASH_PROFIT", "pullback": "PULLBACK_CASH", "breakout": "BREAKOUT_DIST",
    "pct": "PULLBACK_PCT", "trail": "PULLBACK_PCT", "percent": "PULLBACK_PCT",
    "hookmode": "HOOK_MODE", "hookport": "HOOK_PORT", "hookhost": "HOOK_HOST",
    "hooksecret": "HOOK_SECRET", "hookips": "HOOK_ALLOW_IPS", "hookon": "HOOK_ENABLED",
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
    import broker_paper  # virtual account on real prices: no broker, no KYC, no cost
except Exception as _e:
    broker_paper = None
    log.warning(f"broker_paper unavailable: {_e}")

try:
    import broker_capital  # free direct execution, no paid bridge
except Exception as _e:
    broker_capital = None
    log.warning(f"broker_capital unavailable: {_e}")

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
# Trades the user has taken over by attaching his own take profit in the platform.
handed_over_ids = set()
# Last seen state of every bot position, so a close done at the broker is noticed.
tracked_positions = {}
hook_last, hook_count = None, 0
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
                    # systemd reads the LAST assignment, so a leftover duplicate below would
                    # silently win and the new value would never take effect. Drop the extras.
                    if replaced:
                        continue
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


async def validate_metaapi_token(candidate, account_id=None):
    """Prove a new MetaApi token really opens this account before it replaces the old one."""
    account_id = account_id if account_id is not None else ACCOUNT_ID
    probe = MetaApi(token=candidate)
    try:
        acct = await asyncio.wait_for(probe.metatrader_account_api.get_account(account_id), timeout=30.0)
        return None, getattr(acct, "state", "?")
    except Exception as e:
        return str(e), None
    finally:
        try:
            await asyncio.wait_for(probe.close(), timeout=5.0)
        except Exception:
            pass


async def list_metaapi_accounts(token):
    """Every trading account this token can open.

    Changing broker means a new account ID, and copying that ID off the MetaApi site on a
    phone is exactly where this setup used to stall. The token already knows the answer,
    so ask it instead of asking the user.
    """
    probe = MetaApi(token=token)
    try:
        accounts = await asyncio.wait_for(
            probe.metatrader_account_api.get_accounts_with_infinite_scroll_pagination(), timeout=45.0)
        return None, [{
            "id": a.id,
            "name": getattr(a, "name", "") or "",
            "state": getattr(a, "state", "?"),
            "server": getattr(a, "server", "") or "",
            "login": getattr(a, "login", "") or "",
        } for a in accounts]
    except Exception as e:
        return str(e), []
    finally:
        try:
            await asyncio.wait_for(probe.close(), timeout=5.0)
        except Exception:
            pass


def accounts_block(accounts):
    """Render discovered accounts so the user can tap-copy the one he wants."""
    out = []
    for a in accounts:
        label = a["name"] or a["login"] or "حساب"
        out.append(f"• *{label}* — `{a['server']}`\n  `/setkey account {a['id']}`")
    return "\n".join(out)


ACCOUNT_STATE_AR = {
    "UNDEPLOYED": "مطفي عند MetaApi",
    "DEPLOYING": "قيد التجهيز الآن",
    "DEPLOYED": "مجهّز وشغال",
    "UNDEPLOYING": "يُطفأ الآن",
    "DRAFT": "ناقص إعدادات عند MetaApi",
}
BROKER_LINK_AR = {
    "CONNECTED": "✅ متصل بالوسيط",
    "DISCONNECTED": "❌ غير متصل بالوسيط",
    "DISCONNECTED_FROM_BROKER": "❌ الوسيط رافض الدخول — تأكد من رقم الحساب وكلمة السر والسيرفر",
}


async def metaapi_account_line():
    """What MetaApi itself says about the account.

    When the bot cannot connect, the only question that matters is whether the account is
    still waking up or whether the broker is refusing it. Those need very different answers
    from the user, and the timeout message alone does not tell them apart.
    """
    token = os.environ.get("META_API_TOKEN", "")
    if not token or not ACCOUNT_ID:
        return None
    probe = MetaApi(token=token)
    try:
        acct = await asyncio.wait_for(probe.metatrader_account_api.get_account(ACCOUNT_ID), timeout=30.0)
        state = getattr(acct, "state", "?")
        status = getattr(acct, "connection_status", "?")
        return (f"• حالة الحساب: `{state}` — {ACCOUNT_STATE_AR.get(state, state)}\n"
                f"• الربط بالوسيط: {BROKER_LINK_AR.get(status, status)}")
    except Exception as e:
        return f"• ما كدرت أقرأ حالة الحساب: `{str(e)[:150]}`"
    finally:
        try:
            await asyncio.wait_for(probe.close(), timeout=5.0)
        except Exception:
            pass


async def broker_check():
    """One place that answers 'is the bot actually able to trade right now?'"""
    lines = [f"🔎 *فحص الاتصال* — المنصة: `{BROKER}`"]
    if connection is None:
        lines.append("❌ ماكو اتصال حالياً.")
        if last_connection_error:
            # the MetaApi billing advice only makes sense while MetaApi is the platform
            hint = billing_hint(last_connection_error) if BROKER == "metaapi" else None
            lines.append(hint or f"آخر خطأ: `{last_connection_error[:250]}`")
        elif BROKER == "capital" and (broker_capital is None or not broker_capital.configured()):
            lines.append("لسه ما ربطت الحساب. أرسل:\n`/capital <مفتاح> <إيميل> <كلمة سر>`")
        elif saver_sleeping:
            lines.append("😴 الحساب نائم لتوفير الكلفة — أي زر تداول يرجّعه.")
        if BROKER == "metaapi":
            state_line = await metaapi_account_line()
            if state_line:
                lines.append(state_line)
                if "DEPLOYING" in state_line:
                    lines.append("⏳ خليه يكمّل التجهيز — ممكن ياخذ لحد ١٠ دقائق بأول مرة.")
        return "\n".join(lines)
    try:
        acc = await asyncio.wait_for(connection.get_account_information(), timeout=10)
        price = await asyncio.wait_for(connection.get_symbol_price(symbol=SYMBOL), timeout=10)
        pos = await asyncio.wait_for(connection.get_positions(), timeout=10)
        ours = [p for p in pos if p.get("magic") in (MAGIC_BUY, MAGIC_SELL)]
        floating = round(float(acc.get("equity", 0)) - float(acc.get("balance", 0)), 2)
        lines += [
            "✅ الاتصال شغال",
            f"• الرصيد: `{acc.get('balance')} {acc.get('currency','')}` | السيولة: `{acc.get('equity')}`",
            f"• الربح/الخسارة العائمة: `{floating}$`",
            f"• السعر: bid `{price.get('bid')}` / ask `{price.get('ask')}`",
            f"• صفقات مفتوحة: `{len(pos)}` منها `{len(ours)}` للبوت"
            + (f" — `{sum(1 for p in ours if user_took_over(p))}` سلّمتها لك" if any(user_took_over(p) for p in ours) else ""),
            f"• الوضع: {mode_label()}",
            f"• قاعدة الحصاد: ما يغلق قبل `{MIN_CASH_PROFIT}$`، وبعدها يغلق إذا رجع {trail_rule_line()}",
        ]
        guarded = sum(1 for p in ours if has_stop_loss(p) and not user_took_over(p))
        if guarded:
            lines.append(f"• `{guarded}` صفقة عليها ستوب لوز منك — لسه تحت إدارتي عادي 🛡")
        if datafeed:
            lines.append(f"• {datafeed.calibration_line()}")
        if intel:
            lines.append(f"• Claude: {'✅' if intel.claude_ready() else '❌ غير مضبوط'}")
    except Exception as e:
        lines.append(f"⚠️ الاتصال موجود بس الاستعلام فشل: `{str(e)[:250]}`")
    return "\n".join(lines)


async def handle_go_live():
    """/live — one command that puts the bot back on automatic execution through MetaApi.

    Switching broker used to mean editing .env over SSH: new account ID, new symbol name,
    SIGNAL off, BROKER back to metaapi. All of that is decided here from the token itself.
    """
    token = os.environ.get("META_API_TOKEN", "")
    if not token:
        await send_long("🔑 ناقص توكن MetaApi.\nأرسل: `/setkey metaapi <التوكن>`\n"
                        "وأنا أجيب رقم الحساب لوحدي بعدها.", markup=keyboard())
        return
    await send_long("⏳ أتأكد من الحساب قبل ما أحوّل للتنفيذ الآلي...")
    err, accounts = await list_metaapi_accounts(token)
    if err:
        await send_long(f"❌ ما كدرت أوصل MetaApi: `{err[:300]}`\n\n"
                        "إذا التوكن قديم أرسل: `/setkey metaapi <التوكن الجديد>`", markup=keyboard())
        return
    if not accounts:
        await send_long("⚠️ التوكن شغال بس ماكو أي حساب تداول مربوط بيه.\n"
                        "افتح MetaApi ← Accounts ← Add account واربط حساب MT5 الجديد، ثم أرسل /live.",
                        markup=keyboard())
        return
    chosen = next((a for a in accounts if a["id"] == ACCOUNT_ID), None)
    if chosen is None and len(accounts) == 1:
        chosen = accounts[0]
    if chosen is None:
        await send_long(f"عندك `{len(accounts)}` حسابات — اختر واحد وأرسل سطره، وبعدها أرسل /live:\n\n"
                        + accounts_block(accounts), markup=keyboard())
        return
    write_env_value("ACCOUNT_ID", chosen["id"])
    write_env_value("SIGNAL", "0")
    write_env_value("BROKER", "metaapi")
    label = chosen["name"] or chosen["login"] or chosen["id"]
    await send_long(
        "⚡ *رجعنا للتنفيذ الآلي*\n\n"
        f"• الحساب: *{label}* (`{chosen['server']}`)\n"
        f"• حالته: `{chosen['state']}`\n"
        f"• اللوت: `{TRADE_LOT}`\n"
        f"• أداة الذهب: `{SYMBOL}`\n\n"
        "البوت يفتح ويغلق الصفقات بنفسه من الآن — ما تحتاج تلمس المنصة.\n"
        "إذا اسم أداة الذهب مختلف عند وسيطك الجديد، أرسل `/gold` بعد ما يرجع وأنا أضبطه.\n\n"
        "♻️ أعيد التشغيل الآن...")
    os._exit(0)


GOLD_CROSSES = ("EUR", "GBP", "AUD", "JPY", "CHF", "CNH", "SGD", "HKD", "CAD", "NZD")


async def handle_gold_symbol():
    """/gold — ask the broker what gold is called here, instead of guessing the name."""
    if connection is None:
        await send_long("ماكو اتصال بالحساب حالياً — أرسل /check وشوف السبب.", markup=keyboard())
        return
    if not hasattr(connection, "get_symbols"):
        await send_long("هذا الأمر يشتغل بوضع MetaApi فقط. أرسل /live أول.", markup=keyboard())
        return
    await send_long("⏳ أدوّر على أداة الذهب بحسابك...")
    try:
        symbols = await asyncio.wait_for(connection.get_symbols(), timeout=30)
    except Exception as e:
        await send_long(f"⚠️ ما كدرت أجيب قائمة الأدوات: `{str(e)[:250]}`", markup=keyboard())
        return
    matches = [s for s in symbols if "XAU" in s.upper() or "GOLD" in s.upper()]
    if not matches:
        await send_long("⚠️ ما لكيت أي أداة ذهب بهذا الحساب. تأكد إن الذهب مفعّل عند الوسيط.",
                        markup=keyboard())
        return
    # XAUEUR and friends are gold too, but not the one we trade.
    best = [s for s in matches if not any(c in s.upper().replace("XAU", "") for c in GOLD_CROSSES)] or matches
    if len(best) > 1:
        lines = "\n".join(f"• `/setkey gold {s}`" for s in best[:12])
        await send_long(f"لكيت أكثر من أداة ذهب — اختر واحدة وأرسل سطرها:\n\n{lines}", markup=keyboard())
        return
    found = best[0]
    try:  # a name that does not quote is the wrong name, however right it looks
        price = await asyncio.wait_for(connection.get_symbol_price(symbol=found), timeout=15)
    except Exception as e:
        await send_long(f"⚠️ لكيت `{found}` بس ما طلّع سعر: `{str(e)[:200]}`", markup=keyboard())
        return
    if found == SYMBOL:
        await send_long(f"✅ اسم أداة الذهب صحيح أصلاً: `{SYMBOL}` — السعر `{price.get('bid')}`.",
                        markup=keyboard())
        return
    write_env_value("SYMBOL", found)
    await send_long(f"✅ أداة الذهب عند وسيطك اسمها `{found}` (السعر `{price.get('bid')}`) — ضبطتها.\n"
                    "♻️ أعيد التشغيل الآن...")
    os._exit(0)


async def handle_capital_setup(text, chat_id, message_id):
    """/capital <api_key> <email> <password> - verify, self-configure, switch over, restart."""
    parts = text.split()
    if len(parts) < 4:
        await send_long("الصيغة:\n`/capital <المفتاح> <الإيميل> <كلمة سر الـ API>`\n\n"
                        "المفتاح من capital.com ← Settings ← API integrations.")
        return
    api_key, email, password = parts[1], parts[2], " ".join(parts[3:])
    try:  # the credentials must not survive in the chat, whatever happens next
        await asyncio.wait_for(bot.delete_message(chat_id=chat_id, message_id=message_id), timeout=5.0)
    except Exception as e:
        log.warning(f"could not delete credentials message: {e}")

    if broker_capital is None:
        await send_long("⚠️ وحدة Capital.com غير موجودة. أرسل /update أولاً.")
        return

    await send_long("⏳ أفحص الحساب وأضبط كل شي تلقائياً...")
    os.environ.update({"CAPITAL_API_KEY": api_key, "CAPITAL_EMAIL": email,
                       "CAPITAL_PASSWORD": password, "CAPITAL_DEMO": os.environ.get("CAPITAL_DEMO", "1")})
    broker_capital.refresh_keys()

    probe = broker_capital.CapitalBroker()
    try:
        await asyncio.wait_for(probe.connect(), timeout=45)
        acc = await probe.get_account_information()
        price = await probe.get_symbol_price()
        rules = await probe.dealing_rules()
        candles = await probe.get_candles(SYMBOL, "15m", 50)
    except Exception as e:
        await send_long(f"❌ ما نجح الاتصال، وما غيّرت شي.\n`{str(e)[:400]}`\n\n"
                        "تأكد من: المفتاح، الإيميل، وكلمة سر الـ API (مو كلمة سر الدخول).", markup=keyboard())
        return
    finally:
        try:
            await probe.close()
        except Exception:
            pass

    size = max(rules["min_size"], 0.0) or 1.0
    for key, value in (("CAPITAL_API_KEY", api_key), ("CAPITAL_EMAIL", email),
                       ("CAPITAL_PASSWORD", password), ("CAPITAL_EPIC", rules["epic"]),
                       ("CAPITAL_TRADE_SIZE", str(size)), ("CAPITAL_DEMO", os.environ.get("CAPITAL_DEMO", "1")),
                       ("BROKER", "capital")):
        write_env_value(key, value)

    demo = os.environ.get("CAPITAL_DEMO", "1") not in ("0", "false", "no")
    await send_long(
        f"✅ *تم الربط بنجاح* ({'حساب تجريبي' if demo else 'حساب حقيقي'})\n\n"
        f"• الأداة: `{rules['epic']}` — {rules['name']}\n"
        f"• الرصيد: `{acc['balance']} {acc['currency']}` | السيولة: `{acc['equity']}`\n"
        f"• السعر الآن: bid `{price['bid']}` / ask `{price['ask']}`\n"
        f"• الشموع: `{len(candles)}` شمعة ١٥ دقيقة ✓\n"
        f"• حجم الصفقة: `{size}` (الحد الأدنى المسموح، ضبطته تلقائياً)\n"
        + (f"• أقل مسافة ستوب مسموحة: `{rules['min_stop_distance']}`\n" if rules.get("min_stop_distance") else "")
        + "\n♻️ أعيد التشغيل الآن على Capital.com — انتظر رسالة تأكيد الاتصال.")
    os._exit(0)


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
        await send_long("⏳ أفحص التوكن الجديد وأدوّر على حساباتك قبل ما أستبدل القديم...")
        err, accounts = await list_metaapi_accounts(value)
        if err:
            await forget_message()
            await send_long(f"❌ التوكن الجديد ما اشتغل، خليت القديم مثل ما هو.\n`{err[:300]}`", markup=keyboard())
            return
        if not accounts:
            await forget_message()
            await send_long("⚠️ التوكن صحيح بس ماكو أي حساب تداول مربوط بيه.\n"
                            "افتح MetaApi ← Accounts ← Add account واربط حساب MT5، ثم أعد الأمر.",
                            markup=keyboard())
            return
        try:
            shutil.copy2(ENV_PATH, ENV_PATH + ".bak")
        except Exception as e:
            log.warning(f"env backup failed: {e}")
        write_env_value(key, value)
        await forget_message()
        # A new broker means a new account ID. Pick it automatically when the answer is
        # unambiguous, and only ask when the token really does open more than one account.
        chosen = next((a for a in accounts if a["id"] == ACCOUNT_ID), None)
        if chosen is None and len(accounts) == 1:
            chosen = accounts[0]
        if chosen is None:
            await send_long(
                "✅ التوكن صحيح وانحفظ.\n\n"
                f"لكن عندك `{len(accounts)}` حسابات — اختر واحد وأرسل سطره:\n\n"
                + accounts_block(accounts), markup=keyboard())
            return
        write_env_value("ACCOUNT_ID", chosen["id"])
        label = chosen["name"] or chosen["login"] or chosen["id"]
        await send_long(
            f"✅ التوكن صحيح وانحفظ، وربطته بحسابك *{label}* (`{chosen['server']}`).\n"
            f"حالة الحساب: `{chosen['state']}`\n"
            "♻️ أعيد التشغيل الآن.\n\n"
            "🔒 بعد ما يرجع البوت ويكتب لك ✅ تم الاتصال، امسح التوكن القديم من لوحة MetaApi.")
        os._exit(0)

    try:
        write_env_value(key, value)
        if key in ("MIN_CASH_PROFIT", "PULLBACK_CASH"):
            # Remember that this number was chosen, so /lot does not rescale it away later.
            write_env_value("PROFIT_PINNED", "1")
        if intel is not None:
            intel.refresh_keys()
        if broker_capital is not None:
            broker_capital.refresh_keys()
    except Exception as e:
        await send_long(f"⚠️ فشل الحفظ: `{e}`")
        return
    await forget_message()
    # These are read once at import into module-level constants, so writing .env alone would
    # save the value and change nothing until the next restart. Restart now instead.
    if key in ("BROKER", "FORCE_IPV4", "ACCOUNT_ID", "SYMBOL", "LEAN", "SIGNAL",
               "TRADE_LOT", "MIN_CASH_PROFIT", "PULLBACK_CASH", "PULLBACK_PCT", "BREAKOUT_DIST"):
        await send_long(f"🔁 تم ضبط *{SETTABLE_KEYS[key]}* على `{value}`. أعيد التشغيل الآن...")
        os._exit(0)
    status = intel.keys_status() if intel is not None else ""
    await send_long(f"✅ تم حفظ *{SETTABLE_KEYS[key]}* وحذف الرسالة.\n\n{status}", markup=keyboard())


CODE_FILES = ("range_harvester.py", "intel.py", "datafeed.py", "broker_capital.py", "broker_paper.py")


def self_update():
    """Download the newest code, compile it, swap it in. Returns an error string or None.

    Every failure has to come back as a message: an update that dies quietly leaves the
    user staring at '⏳ downloading…' forever, which is exactly what used to happen."""
    files = CODE_FILES + ("requirements.txt",)
    tmpdir = os.path.join(BASE_DIR, ".update")
    try:
        os.makedirs(tmpdir, exist_ok=True)
        # Same IPv6 trap as the API calls: GitHub resolves to an address this host cannot
        # route, and curl then hangs until the timeout instead of failing fast.
        curl = ["curl", "-fsSL", "--connect-timeout", "15", "--retry", "2", "--retry-delay", "2"]
        if os.environ.get("FORCE_IPV4", "0") in ("1", "true", "yes"):
            curl.append("-4")
        for name in files:
            try:
                got = subprocess.run(curl + [f"{RAW_BASE}/{name}", "-o", os.path.join(tmpdir, name)],
                                     capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                return (f"⚠️ تحميل `{name}` تجاوز الوقت — الاتصال بـ GitHub بطيء أو محجوب.\n"
                        "جرب `/setkey ipv4 1` ثم `/update` مرة ثانية.")
            if got.returncode != 0:
                return f"⚠️ فشل التحميل ({name}): `{got.stderr.strip()[:200]}`"

        py = sys.executable
        new_req = os.path.join(tmpdir, "requirements.txt")
        old_req = os.path.join(BASE_DIR, "requirements.txt")
        # Installing packages is the slow part, and it is pointless when nothing changed.
        needs_pip = not os.path.exists(old_req) or open(new_req).read() != open(old_req).read()
        if needs_pip:
            try:
                pip = subprocess.run([py, "-m", "pip", "install", "-q", "-r", new_req],
                                     capture_output=True, text=True, timeout=600)
                if pip.returncode != 0:
                    log.warning(f"pip install failed: {pip.stderr[-300:]}")
            except subprocess.TimeoutExpired:
                log.warning("pip install timed out; continuing with the packages already installed")

        comp = subprocess.run([py, "-m", "py_compile"] + [os.path.join(tmpdir, n) for n in CODE_FILES],
                              capture_output=True, text=True, timeout=180)
        if comp.returncode != 0:
            return f"⚠️ الكود الجديد فيه خطأ، أُلغي التحديث:\n`{comp.stderr.strip()[:400]}`"
        for name in files:
            shutil.copy2(os.path.join(tmpdir, name), os.path.join(BASE_DIR, name))
        return None
    except subprocess.TimeoutExpired as e:
        return f"⚠️ التحديث تجاوز الوقت المسموح عند: `{str(e)[:150]}`\nجرب مرة ثانية."
    except Exception as e:
        log.error(f"self_update failed: {e}", exc_info=True)
        return f"⚠️ فشل التحديث: `{str(e)[:300]}`\nالبوت مستمر بالنسخة الحالية."


HELP_TEXT = (
    "🤖 *أوامر البوت*\n"
    "• /start — لوحة التحكم\n"
    "• `/setkey claude sk-ant-...` — حفظ مفتاح Claude (تُحذف الرسالة تلقائياً)\n"
    "• `/capital <مفتاح> <إيميل> <كلمة سر>` — ربط Capital.com وضبط كل شي تلقائياً\n"
    "• /check — فحص الاتصال والحساب والسعر\n"
    "• /hook — ربط TradingView: الرابط والحالة (`/hook trade` للتنفيذ، `/hook watch` للمراقبة)\n"
    "• /live — رجوع للتنفيذ الآلي على MetaApi (يلقى حسابك لوحده)\n"
    "• /gold — يسأل وسيطك شنو اسم أداة الذهب ويضبطها\n"
    "• `/setkey account <id>` — رقم الحساب بـ MetaApi\n"
    "• `/lot 0.01` — تغيير حجم اللوت (والحدود تتعدل معه تلقائياً)\n"
    "• /signal — وضع الإشارات: البوت ينبّه وأنت تنفّذ بـ MT5 (مجاني تماماً)\n"
    "• `/entry 4468.5` — تصحيح سعر دخولك الحقيقي (أضف sell للبيع)\n"
    "• 🤝 *تسليم صفقة:* حط هدف (TP) على أي صفقة للبوت من المنصة وهو يرفع يده عنها نهائياً — شيل الهدف ويرجع لها\n"
    "• 🛡 *ستوب لوز:* حطه بحرية — البوت يبقى يتحكم بالصفقة عادي، وإذا ضرب الستوب يخبرك ويوقف ذاك الجانب\n"
    "• /paper — تشغيل حساب تجريبي داخلي بدون وسيط ولا كلفة\n"
    "• /report — أداء الحساب التجريبي (ربح/خسارة، نسبة نجاح)\n"
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
    rows = [
        [InlineKeyboardButton("📊 حالة النطاق والأرباح", callback_data="status"), InlineKeyboardButton("🥇 السعر اللحظي", callback_data="price")],
        [InlineKeyboardButton("🟢 ارتكاز شراء جديد", callback_data="anchor_buy"), InlineKeyboardButton("🔴 ارتكاز بيع جديد", callback_data="anchor_sell")],
        [InlineKeyboardButton("⚔️ ارتكاز مزدوج (شراء + بيع)", callback_data="anchor_dual")],
    ]
    if not LEAN:   # trading-only mode hides everything that is not an order
        rows += [
            [InlineKeyboardButton("🧠 استشارة السوق", callback_data="advisory"),
             InlineKeyboardButton("📰 آخر الأخبار", callback_data="news")],
            [InlineKeyboardButton("🎯 صفقة اليوم", callback_data="daily_trade")],
        ]
    rows += [
        [InlineKeyboardButton(mode_label(), callback_data="toggle_mode")],
        [InlineKeyboardButton(status, callback_data="toggle_bot")],
        [InlineKeyboardButton("🚨 إغلاق جميع صفقات البوت", callback_data="close_all")],
    ]
    return InlineKeyboardMarkup(rows)


async def safe_reply(coro_func, **kwargs):
    for attempt in range(2):
        try:
            return await asyncio.wait_for(coro_func(**kwargs), timeout=6.0)
        except Exception as e:
            log.warning(f"reply attempt {attempt+1} failed: {e}")
            await asyncio.sleep(0.4)
    return None


def act(action, detail=""):
    """In signal mode the bot has no hands: it tells the user exactly what to tap in MT5."""
    if not SIGNAL:
        return None
    return (f"🔔🔔 *نفّذ الآن بالمنصة*\n\n*{action}*\n{detail}\n\n"
            f"• الأداة: `{SYMBOL}`  • اللوت: `{TRADE_LOT}`\n"
            f"⏱ نفّذها بسرعة — السعر يتحرك.")


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
    if connection is not None and hasattr(connection, "get_candles"):
        try:
            candles = await asyncio.wait_for(connection.get_candles(symbol, timeframe, limit), timeout=25.0)
            if candles and len(candles) >= 30:
                return candles
        except Exception as e:
            log.warning(f"broker candles failed ({symbol} {timeframe}), using free feed: {e}")
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
    if BROKER != "metaapi":
        return connection is not None
    wake_until = time.time() + WAKE_GRACE_MINUTES * 60
    if connection:
        return True
    # No point making the user wait two minutes for a platform we already know is refusing us.
    blocked = billing_hint(last_connection_error) if last_connection_error else None
    if blocked:
        await notify("🚫 *التنفيذ متوقف* — منصة التداول رافضة الاتصال.\n\n" + blocked +
                     "\n\nأو بدّل لمنصة مجانية بأمر واحد:\n`/capital <مفتاح> <إيميل> <كلمة سر>`")
        return False
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
    if BROKER != "metaapi":
        log.info("cost governor off: this broker has no deployed-hours bill")
        return
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

            if BROKER == "paper":
                if broker_paper is None:
                    await asyncio.sleep(10)
                    continue
                conn = broker_paper.PaperBroker()
                await asyncio.wait_for(conn.connect(), timeout=30)
                connection = conn
                last_price_ok = time.time()
                fail_streak, hinted = 0, None
                acc = await conn.get_account_information()
                await notify(f"📄 **الحساب التجريبي الداخلي جاهز** — الرصيد `{acc['balance']}$`.\n"
                             "نفس الاستراتيجية، أسعار ذهب حقيقية، بدون أي وسيط.")
                continue

            if BROKER == "capital":
                if broker_capital is None or not broker_capital.configured():
                    await asyncio.sleep(10)
                    continue
                log.info("Connecting Capital.com...")
                conn = broker_capital.CapitalBroker()
                await asyncio.wait_for(conn.connect(), timeout=45)
                connection = conn
                last_price_ok = time.time()
                fail_streak, hinted = 0, None
                await notify("✅ **تم الاتصال بحساب Capital.com بنجاح.**")
                continue

            log.info("Connecting MetaApi...")
            api_instance = MetaApi(token=META_API_TOKEN)
            account_instance = await api_instance.metatrader_account_api.get_account(ACCOUNT_ID)
            cold = account_instance.state != "DEPLOYED"
            if cold:
                await account_instance.deploy()
                await notify("⏳ **الحساب يجهّز عند MetaApi لأول مرة** — ياخذ لحد ١٠ دقائق.\n"
                             "خليه وأنا أكتب لك أول ما يخلص.")
                await account_instance.wait_deployed(timeout_in_seconds=DEPLOY_TIMEOUT_SECS)
            await account_instance.wait_connected(timeout_in_seconds=DEPLOY_TIMEOUT_SECS)
            conn = account_instance.get_rpc_connection()
            await conn.connect()
            await conn.wait_synchronized(timeout_in_seconds=DEPLOY_TIMEOUT_SECS)
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
            # An empty balance or a rejected token is a wall, not a blip: retrying every few
            # seconds cannot fix it and only buries the user in repeats of the same message.
            await asyncio.sleep(600 if hint else min(5 * fail_streak, 60))


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
        await notify(act("🟢 افتح صفقة شراء (BUY)", f"• السعر التقريبي: `{buy_anchor}`")
                     or f"🟢 **ارتكاز شراء جديد ({src}):**\n• السعر: `{buy_anchor}`")
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
        await notify(act("🔴 افتح صفقة بيع (SELL)", f"• السعر التقريبي: `{sell_anchor}`")
                     or f"🔴 **ارتكاز بيع جديد ({src}):**\n• السعر: `{sell_anchor}`")
    except Exception as e:
        log.error(f"Sell err: {e}", exc_info=True)


def give_back_limit(peak):
    """How much of the peak profit a trade may surrender before it is harvested.

    A share of the peak, not a fixed sum: a trade that ran to +40$ deserves more room than
    one sitting at +6$, and a fixed give-back would either strangle the big move or let the
    small one round-trip. MIN_CASH_PROFIT still gates the close, so the bot never harvests
    below the floor however small the give-back works out to be.
    """
    if PULLBACK_PCT > 0:
        return round(peak * PULLBACK_PCT / 100.0, 2)
    return PULLBACK_CASH


def trail_rule_line():
    """One line that states the harvest rule exactly as it will behave."""
    if PULLBACK_PCT > 0:
        return (f"`{round(PULLBACK_PCT, 2)}%` من أعلى ربح توصله الصفقة "
                f"(مثلاً قمة `20$` ← يغلق عند `{round(20 * (1 - PULLBACK_PCT / 100.0), 2)}$`)")
    return f"`{PULLBACK_CASH}$` من أعلى ربح توصله الصفقة"


def user_took_over(p):
    """A take profit on a bot trade can only have come from the user.

    The bot opens every position bare — magic number only, no TP — so a TP showing up on
    one means the user set a target by hand in MT5. Two owners closing the same trade is
    how a position gets closed at the wrong moment, so the bot steps off instead.

    A stop loss deliberately does NOT count. A stop and the harvest rule do not compete:
    the stop only decides how much the trade may lose, the harvest only when a winner is
    taken, so the bot goes on managing a trade the user has protected.
    """
    tp = p.get("takeProfit")
    try:
        return tp is not None and float(tp) > 0
    except (TypeError, ValueError):
        return False


def has_stop_loss(p):
    sl = p.get("stopLoss")
    try:
        return sl is not None and float(sl) > 0
    except (TypeError, ValueError):
        return False


async def note_external_closes(mine):
    """Report a bot trade that disappeared without the bot closing it.

    A stop loss the user attached fires at the broker, not here. Without this the position
    would simply vanish: no message, no tally, and the side left holding an anchor it never
    re-enters — the bot would look alive while doing nothing at all.
    """
    global tracked_positions, buy_anchor, sell_anchor
    global buy_waiting, sell_waiting, peak_buy_profit, peak_sell_profit
    live = {p.get("id") for p in mine}
    for pid, info in list(tracked_positions.items()):
        if pid in live:
            info["missing"] = 0
            continue
        # One empty read can be a resync, not a close. Confirm it before saying anything.
        info["missing"] = info.get("missing", 0) + 1
        if info["missing"] < 2:
            continue
        tracked_positions.pop(pid, None)
        is_buy, side = info["buy"], ("الشراء" if info["buy"] else "البيع")
        if is_buy:
            buy_anchor, buy_waiting, peak_buy_profit = None, False, 0.0
        else:
            sell_anchor, sell_waiting, peak_sell_profit = None, False, 0.0
        await notify(
            f"🛑 *صفقة {side} انغلقت من المنصة مو مني*\n\n"
            f"• غالباً الستوب لوز اللي حاططه عليها، أو أغلقتها بيدك.\n"
            f"• آخر نتيجة شفتها عليها: `{round(info['profit'], 2)}$` — تقريبية، الرقم الدقيق بكشف حسابك.\n"
            f"• ما أعيد الدخول تلقائياً بعد ستوب، فجانب {side} صار فاضي.\n\n"
            "اضغط ارتكاز جديد لما تريد تكمل.")
    for p in mine:
        pid = p.get("id")
        entry = tracked_positions.setdefault(pid, {"buy": p.get("magic") == MAGIC_BUY, "profit": 0.0})
        entry["missing"] = 0
        try:
            entry["profit"] = float(p.get("unrealizedProfit", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass


async def review_handovers(mine):
    """Announce a trade the user has taken over, and one he has handed back."""
    global handed_over_ids, buy_anchor, sell_anchor
    global buy_waiting, sell_waiting, peak_buy_profit, peak_sell_profit
    for p in mine:
        pid = p.get("id")
        is_buy = p.get("magic") == MAGIC_BUY
        side = "الشراء" if is_buy else "البيع"
        if user_took_over(p):
            if pid in handed_over_ids:
                continue
            handed_over_ids.add(pid)
            # The side goes idle rather than re-entering: the user is driving this one now.
            if is_buy:
                buy_anchor, buy_waiting, peak_buy_profit = None, False, 0.0
            else:
                sell_anchor, sell_waiting, peak_sell_profit = None, False, 0.0
            await notify(
                f"🤝 *سلّمتك الصفقة* — {side} `{p.get('volume')}`\n\n"
                f"• حطّيت عليها هدف `{p.get('takeProfit')}` بنفسك، فرفعت يدي عنها تماماً.\n"
                "• ما أغلقها ولا أحصدها، ولا يشملها زر «إغلاق جميع صفقات البوت».\n"
                f"• جانب {side} صار فاضي — اضغط ارتكاز جديد وقت ما تريد.\n\n"
                "تريدني أرجع أتحكم بيها؟ شيل الهدف من المنصة وأرجع لها فوراً.")
        elif pid in handed_over_ids:
            handed_over_ids.discard(pid)
            await notify(f"↩️ *رجعت أتحكم بصفقة {side}* — شلت الهدف عنها، فرجعت تحت إدارتي بنفس الحدود.")
    handed_over_ids &= {p.get("id") for p in mine}   # a closed trade must not linger here


# --- TradingView webhook -------------------------------------------------------------
BUY_WORDS = ("buy", "long", "شراء")
SELL_WORDS = ("sell", "short", "بيع")
CLOSE_WORDS = ("close", "exit", "flat", "اغلاق", "إغلاق", "اغلق", "أغلق")
HOOK_LABEL = {"buy": "🟢 شراء", "sell": "🔴 بيع", "close": "🚪 إغلاق"}


def read_signal(body):
    """Work out what an alert is asking for, from JSON or from plain text.

    The user writes the alert message himself in TradingView, so this reads intent rather
    than a fixed format: an Arabic 'فرصة شراء', an English 'buy', or a JSON payload all
    arrive here. A message carrying both sides is refused rather than guessed at — opening
    the wrong direction costs real money.
    """
    text = (body or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                text = " ".join(str(v) for v in data.values())
        except Exception:
            pass
    low = text.lower()
    if any(w in low for w in CLOSE_WORDS):
        return "close", text
    hit_buy = any(w in low for w in BUY_WORDS)
    hit_sell = any(w in low for w in SELL_WORDS)
    if hit_buy == hit_sell:          # both, or neither: never guess a direction
        return None, text
    return ("buy" if hit_buy else "sell"), text


async def close_bot_positions():
    """Close every position the bot owns and still controls. Returns (closed, left alone)."""
    if not connection:
        return 0, 0
    closed, skipped = 0, 0
    all_pos = await asyncio.wait_for(connection.get_positions(), timeout=4.0)
    for p in all_pos:
        if p.get("symbol") != SYMBOL or p.get("magic") not in (MAGIC_BUY, MAGIC_SELL):
            continue
        if user_took_over(p):        # the user owns this one now
            skipped += 1
            continue
        try:
            await asyncio.wait_for(connection.close_position(p["id"]), timeout=5.0)
            tracked_positions.pop(p["id"], None)
            closed += 1
        except Exception as e:
            log.warning(f"failed closing {p.get('id')}: {e}")
    return closed, skipped


async def on_hook_signal(body, ip):
    """Act on one alert. Runs after the HTTP reply, never inside it."""
    global hook_last, hook_count
    action, text = read_signal(body)
    short = (text.strip()[:200] or "(رسالة فارغة)")
    hook_count += 1
    hook_last = (datetime.now(timezone.utc).strftime("%H:%M:%S UTC"), action or "غير مفهومة", short[:120])
    log.info(f"webhook signal from {ip}: action={action} body={text[:120]!r}")

    if action is None:
        await notify(f"📡 *وصلت إشارة من TradingView* بس ما عرفت شتريد:\n`{short}`\n\n"
                     "لازم نص التنبيه يحتوي *شراء* أو *بيع* (أو buy / sell) — وحدة منهم مو الثنتين.")
        return
    label = HOOK_LABEL[action]
    if HOOK_MODE != "trade":
        await notify(f"📡 *إشارة {label} من TradingView*\n`{short}`\n\n"
                     "👁 وضع المراقبة — ما نفّذت شي.\n"
                     "لما تتأكد إن الإشارات تجيك صح وبوقتها، أرسل `/hook trade`.")
        return
    if not bot_active:
        await notify(f"📡 إشارة {label} وصلت، بس النظام مطفي من اللوحة — ما نفّذت.")
        return
    if not await wake_for_execution("إشارة TradingView"):
        await notify(f"📡 إشارة {label} وصلت بس ما كدرت أجهّز الحساب بالوقت — ما نفّذت.")
        return
    try:
        if action == "buy":
            await set_new_buy_anchor(manual=True)
        elif action == "sell":
            await set_new_sell_anchor(manual=True)
        else:
            closed, skipped = await close_bot_positions()
            msg = f"📡 إشارة إغلاق من TradingView — أغلقت `{closed}` صفقة."
            if skipped:
                msg += f"\n🤝 تركت `{skipped}` صفقة سلّمتها لك."
            await notify(msg)
    except Exception as e:
        log.error(f"webhook execution failed: {e}", exc_info=True)
        await notify(f"⚠️ إشارة {label} وصلت بس التنفيذ فشل: `{str(e)[:200]}`")


async def hook_server():
    """A tiny HTTP endpoint for TradingView alerts.

    Hand-rolled rather than pulling in a web framework: one route, one method, and the bot
    already has enough moving parts. The reply goes out before any trading work starts,
    because TradingView cancels a request that takes longer than three seconds.
    """
    if not (HOOK_ENABLED and HOOK_SECRET):
        log.info("webhook listener off (disabled or no secret)")
        return
    allow = {ip.strip() for ip in HOOK_ALLOW_IPS.split(",") if ip.strip()}
    want_path = f"/hook/{HOOK_SECRET}"

    async def handle(reader, writer):
        ok, body, ip = False, "", "?"
        try:
            peer = writer.get_extra_info("peername")
            ip = peer[0] if peer else "?"
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split()
            method, path = (parts + ["", ""])[:2]
            length = 0
            for h in lines[1:]:
                if h.lower().startswith("content-length:"):
                    try:
                        length = max(0, min(int(h.split(":", 1)[1].strip()), HOOK_MAX_BODY))
                    except ValueError:
                        length = 0
            if length:
                raw = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
                body = raw.decode("utf-8", "replace")
            ok = (method.upper() == "POST"
                  and hmac.compare_digest(path.strip(), want_path)
                  and (not allow or ip in allow))
            if not ok:
                log.warning(f"webhook rejected from {ip}: {method} {path[:40]}")
            status = b"200 OK" if ok else b"403 Forbidden"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception as e:
            ok = False
            log.warning(f"webhook request from {ip} failed: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass
        if ok:   # answered first, traded second
            asyncio.create_task(on_hook_signal(body, ip))

    try:
        server = await asyncio.start_server(handle, "0.0.0.0", HOOK_PORT)
    except Exception as e:
        log.error(f"webhook listener failed on port {HOOK_PORT}: {e}")
        await notify(f"⚠️ *ما كدرت أفتح منفذ الإشارات* `{HOOK_PORT}`\n`{str(e)[:160]}`\n\n"
                     "منفذ 80 يحتاج صلاحية. الحل بأمر واحد بالسيرفر موجود بالشرح.")
        return
    log.info(f"webhook listening on 0.0.0.0:{HOOK_PORT}{want_path}")
    async with server:
        await server.serve_forever()


def hook_status_text():
    url = f"http://{HOOK_HOST or '<عنوان-السيرفر>'}:{HOOK_PORT}/hook/{HOOK_SECRET or '<السر>'}"
    if HOOK_PORT in (80, 443):
        scheme = "https" if HOOK_PORT == 443 else "http"
        url = f"{scheme}://{HOOK_HOST or '<عنوان-السيرفر>'}/hook/{HOOK_SECRET or '<السر>'}"
    state = ("🚀 *تنفيذ* — الإشارة تفتح صفقة فعلية" if HOOK_MODE == "trade"
             else "👁 *مراقبة* — يخبرك بالإشارة بدون تنفيذ")
    lines = [
        "📡 *ربط TradingView*", "",
        f"• الحالة: {'✅ شغال' if HOOK_ENABLED and HOOK_SECRET else '❌ مطفي'}",
        f"• الوضع: {state}",
        f"• المنفذ: `{HOOK_PORT}`",
        f"• إشارات وصلت: `{hook_count}`",
    ]
    if hook_last:
        when, act, body = hook_last
        lines.append(f"• آخر إشارة: `{when}` → *{act}*\n  `{body}`")
    lines += ["", "*الرابط اللي تحطه بخانة Webhook URL:*", f"`{url}`", "",
              "*نص التنبيه:* اكتب فيه كلمة `شراء` أو `بيع` — وحدة بس.", "",
              "`/hook trade` للتنفيذ · `/hook watch` للمراقبة"]
    return "\n".join(lines)


async def detect_public_ip():
    """The address TradingView has to reach. Asked for once, then stored."""
    cmd = ["curl", "-fsS", "--max-time", "10", "https://api.ipify.org"]
    if os.environ.get("FORCE_IPV4", "0") in ("1", "true", "yes"):
        cmd.insert(1, "-4")
    try:
        got = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=15)
        ip = (got.stdout or "").strip()
        return ip if ip.count(".") == 3 and len(ip) <= 15 else None
    except Exception as e:
        log.warning(f"public ip lookup failed: {e}")
        return None


async def handle_hook_command(text):
    """/hook — show the link and state. /hook trade | watch | off | on — change it."""
    global HOOK_HOST
    arg = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
    if arg in ("trade", "تنفيذ"):
        if HOOK_MODE == "trade":
            await send_long("🚀 وضع التنفيذ مفعّل أصلاً.", markup=keyboard())
            return
        write_env_value("HOOK_MODE", "trade")
        await send_long("🚀 *فعّلت التنفيذ من إشارات TradingView*\n\n"
                        f"أي إشارة شراء أو بيع راح تفتح صفقة حقيقية بـ `{TRADE_LOT}` لوت فوراً.\n"
                        "ترجعه للمراقبة بأمر `/hook watch`.\n\n♻️ أعيد التشغيل...")
        os._exit(0)
    if arg in ("watch", "مراقبة"):
        write_env_value("HOOK_MODE", "watch")
        await send_long("👁 رجعته للمراقبة — يخبرك بالإشارة بدون أي تنفيذ.\n♻️ أعيد التشغيل...")
        os._exit(0)
    if arg in ("off", "stop", "ايقاف", "إيقاف"):
        write_env_value("HOOK_ENABLED", "0")
        await send_long("🛑 أطفيت استقبال الإشارات نهائياً.\n♻️ أعيد التشغيل...")
        os._exit(0)
    if arg in ("on", "تشغيل"):
        write_env_value("HOOK_ENABLED", "1")
        await send_long("✅ شغّلت استقبال الإشارات.\n♻️ أعيد التشغيل...")
        os._exit(0)
    if arg in ("new", "reset", "تجديد"):
        import secrets
        fresh = secrets.token_urlsafe(24)
        write_env_value("HOOK_SECRET", fresh)
        await send_long("🔑 بدّلت السر — الرابط القديم بطل.\n"
                        "بعد ما أرجع أرسل `/hook` وحدّث الرابط بـ TradingView.\n♻️ أعيد التشغيل...")
        os._exit(0)
    if not HOOK_HOST:
        await send_long("⏳ أدوّر على عنوان السيرفر العام...")
        found = await detect_public_ip()
        if found:
            HOOK_HOST = found
            write_env_value("HOOK_HOST", found)
    await send_long(hook_status_text(), markup=keyboard())


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
            mine = [p for p in all_pos if p.get("symbol") == SYMBOL and p.get("magic") in (MAGIC_BUY, MAGIC_SELL)]
            await review_handovers(mine)
            await note_external_closes(mine)
            buy_pos = [p for p in mine if p.get("type") == "POSITION_TYPE_BUY" and p.get("magic") == MAGIC_BUY
                       and not user_took_over(p)]
            sell_pos = [p for p in mine if p.get("type") == "POSITION_TYPE_SELL" and p.get("magic") == MAGIC_SELL
                        and not user_took_over(p)]

            if len(buy_pos) > 0:
                b = buy_pos[-1]
                if buy_anchor is None:
                    buy_anchor = b.get("openPrice", ask)
                curr_profit = float(b.get("unrealizedProfit", 0.0))
                if curr_profit >= MIN_CASH_PROFIT:
                    if curr_profit > peak_buy_profit:
                        peak_buy_profit = curr_profit
                    if curr_profit <= (peak_buy_profit - give_back_limit(peak_buy_profit)):
                        if await safe_close_position(b["id"]):
                            tracked_positions.pop(b["id"], None)
                            total_profit += curr_profit
                            buy_count += 1
                            buy_waiting = True
                            captured = peak_buy_profit
                            peak_buy_profit = 0.0
                            await notify(act("🟢 اغلق صفقة الشراء الآن!", f"• الربح التقديري: `+{round(curr_profit,2)}$` (القمة كانت `+{round(captured,2)}$`)")
                                         or f"🟢 **حصد قمة الشراء!** القمة: `+{round(captured,2)}$` | الإغلاق: `+{round(curr_profit,2)}$`")
                            await asyncio.sleep(1.5)
            elif buy_anchor is not None and buy_waiting:
                if ask >= (buy_anchor + BREAKOUT_DIST):
                    await set_new_buy_anchor(manual=False)
                    await asyncio.sleep(2.0)
                elif ask <= (buy_anchor + 0.25):
                    buy_waiting, peak_buy_profit = False, 0.0
                    await asyncio.wait_for(connection.create_market_buy_order(symbol=SYMBOL, volume=TRADE_LOT, options={"magic": MAGIC_BUY}), timeout=5.0)
                    await notify(act("🟢 افتح شراء (عودة للارتكاز)", f"• الارتكاز: `{buy_anchor}`")
                                 or f"🟢 **فتح شراء جديد لعودة السعر لارتكاز (`{buy_anchor}`)**")
                    await asyncio.sleep(2.0)

            if len(sell_pos) > 0:
                s = sell_pos[-1]
                if sell_anchor is None:
                    sell_anchor = s.get("openPrice", bid)
                curr_profit = float(s.get("unrealizedProfit", 0.0))
                if curr_profit >= MIN_CASH_PROFIT:
                    if curr_profit > peak_sell_profit:
                        peak_sell_profit = curr_profit
                    if curr_profit <= (peak_sell_profit - give_back_limit(peak_sell_profit)):
                        if await safe_close_position(s["id"]):
                            tracked_positions.pop(s["id"], None)
                            total_profit += curr_profit
                            sell_count += 1
                            sell_waiting = True
                            captured = peak_sell_profit
                            peak_sell_profit = 0.0
                            await notify(act("🔴 اغلق صفقة البيع الآن!", f"• الربح التقديري: `+{round(curr_profit,2)}$` (القمة كانت `+{round(captured,2)}$`)")
                                         or f"🔴 **حصد قاع البيع!** القمة: `+{round(captured,2)}$` | الإغلاق: `+{round(curr_profit,2)}$`")
                            await asyncio.sleep(1.5)
            elif sell_anchor is not None and sell_waiting:
                if bid <= (sell_anchor - BREAKOUT_DIST):
                    await set_new_sell_anchor(manual=False)
                    await asyncio.sleep(2.0)
                elif bid >= (sell_anchor - 0.25):
                    sell_waiting, peak_sell_profit = False, 0.0
                    await asyncio.wait_for(connection.create_market_sell_order(symbol=SYMBOL, volume=TRADE_LOT, options={"magic": MAGIC_SELL}), timeout=5.0)
                    await notify(act("🔴 افتح بيع (عودة للارتكاز)", f"• الارتكاز: `{sell_anchor}`")
                                 or f"🔴 **فتح بيع جديد لعودة السعر لارتكاز (`{sell_anchor}`)**")
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
                    closed, skipped = await close_bot_positions()
                    txt = f"🚨 **تم إغلاق `{closed}` من صفقات البوت.**"
                    if skipped:
                        txt += f"\n🤝 تركت `{skipped}` صفقة حاطط عليها هدف بنفسك — هي تحت إدارتك."
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
            elif text.startswith("/capital"):
                await handle_capital_setup(text, cid, update.message.message_id)
            elif text.startswith("/hook"):
                await handle_hook_command(text)
            elif text.startswith("/live"):
                await handle_go_live()
            elif text.startswith("/gold"):
                await handle_gold_symbol()
            elif text.startswith("/paper"):
                write_env_value("BROKER", "paper")
                await send_long("📄 *تحويل إلى الحساب التجريبي الداخلي*\n"
                                "نفس الاستراتيجية بأسعار حقيقية، بدون وسيط ولا كلفة.\n"
                                "♻️ أعيد التشغيل الآن...")
                os._exit(0)
            elif text.startswith("/report"):
                await send_long(broker_paper.report() if broker_paper else "⚠️ الوحدة غير موجودة، أرسل /update أولاً.",
                                markup=keyboard())
            elif text.startswith("/lot"):
                parts = text.split()
                if len(parts) < 2:
                    await send_long(f"اللوت الحالي: `{TRADE_LOT}`\nللتغيير: `/lot 0.01`", markup=keyboard())
                else:
                    try:
                        lot = float(parts[1])
                    except ValueError:
                        lot = 0
                    if not 0 < lot <= 10:
                        await send_long("حجم غير صالح.")
                    else:
                        sc = lot / REF_LOT
                        write_env_value("TRADE_LOT", str(lot))
                        # A threshold the user chose himself is a decision, not a default:
                        # silently rescaling it here once wiped a deliberate 5$ harvest.
                        pinned = os.environ.get("PROFIT_PINNED", "0") in ("1", "true", "yes")
                        if pinned:
                            limits = (f"• حد الحصاد: `{MIN_CASH_PROFIT}$` — خليته مثل ما ضبطته بنفسك\n"
                                      f"• الارتداد: {trail_rule_line()}\n")
                        else:
                            for k in ("MIN_CASH_PROFIT", "PULLBACK_CASH"):
                                os.environ.pop(k, None)
                                write_env_value(k, str(round((2.00 if k == "MIN_CASH_PROFIT" else 0.80) * sc, 2)))
                            limits = (f"• حد الحصاد: `{round(2.00 * sc, 2)}$` (كان 2.00$)\n"
                                      f"• الارتداد: {trail_rule_line()}\n")
                        await send_long(
                            f"✅ *اللوت صار* `{lot}`\n\n"
                            f"والحدود:\n{limits}"
                            f"• الاختراق: `{BREAKOUT_DIST}$` (ما يتغير — مسافة سعر)\n\n"
                            "♻️ أعيد التشغيل...")
                        os._exit(0)
            elif text.startswith("/signal"):
                on = "off" not in text.lower()
                write_env_value("SIGNAL", "1" if on else "0")
                if on:
                    # the bookkeeping runs on free prices; the human is the one who executes
                    write_env_value("BROKER", "paper")
                    await send_long(
                        "🔔 *وضع الإشارات مفعّل*\n\n"
                        "البوت يراقب الذهب بأسعار حقيقية وبنفس استراتيجيتك، ويرسل لك أمر تنفيذ واضح:\n"
                        "«افتح شراء الآن» / «اغلق الآن، الربح +2.2$»\n"
                        "وأنت تنفّذ بضغطتين بتطبيق MT5.\n\n"
                        "✅ بدون MetaApi · بدون اشتراك · بدون توثيق هوية\n"
                        "♻️ أعيد التشغيل الآن...")
                else:
                    await send_long("🔄 أطفيت وضع الإشارات. ♻️ أعيد التشغيل...")
                os._exit(0)
            elif text.startswith("/entry"):
                parts = text.split()
                if len(parts) < 2:
                    await send_long("الصيغة: `/entry 4468.5` — سعر دخولك الحقيقي من المنصة.")
                else:
                    try:
                        price = float(parts[1])
                    except ValueError:
                        await send_long("السعر غير صالح.")
                    else:
                        global buy_anchor, sell_anchor
                        which = "buy" if "sell" not in text.lower() else "sell"
                        if which == "buy":
                            buy_anchor = price
                        else:
                            sell_anchor = price
                        await send_long(f"✅ ضبطت سعر دخول *{'الشراء' if which == 'buy' else 'البيع'}* على `{price}`.\n"
                                        "الأرباح تُحسب من هذا السعر الآن.", markup=keyboard())
            elif text.startswith("/lean"):
                on = "off" not in text.lower()
                write_env_value("LEAN", "1" if on else "0")
                await send_long(("🧹 *وضع التداول فقط*\nألغيت الأخبار والتحليل والصفقة اليومية. "
                                 "أزرار التداول فقط.\nترجعها بأمر `/lean off`."
                                 if on else
                                 "🔄 *رجعت الأخبار والتحليل*."))
                os._exit(0)
            elif text.startswith("/check"):
                await send_long(await broker_check(), markup=keyboard())
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
                await send_long("⏳ جاري سحب آخر نسخة من GitHub... (لحد دقيقتين)")
                try:
                    err = await asyncio.wait_for(asyncio.to_thread(self_update), timeout=900)
                except asyncio.TimeoutError:
                    err = "⚠️ التحديث طوّل أكثر من اللازم وتوقف. البوت مستمر بالنسخة الحالية — جرب `/update` مرة ثانية."
                except Exception as e:
                    err = f"⚠️ فشل التحديث: `{str(e)[:300]}`"
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
    conflicts = 0
    while True:
        try:
            updates = await asyncio.wait_for(bot.get_updates(offset=offset, timeout=10), timeout=20)
            conflicts = 0
            for u in updates:
                offset = u.update_id + 1
                asyncio.create_task(process_update(u))
        except Exception as e:
            # A 409 means someone else is polling this token: the bot can still send, but
            # commands never arrive. Say so instead of failing silently forever.
            if "Conflict" in str(e) or "409" in str(e):
                conflicts += 1
                log.error(f"telegram conflict ({conflicts}): another poller holds this token")
                if conflicts == 5:
                    await notify("⚠️ *في نسخة ثانية من البوت شغالة* على نفس التوكن، فأوامرك ما تصلني.\n"
                                 "شغّل هذا مرة وحدة من Termux:\n"
                                 "`sudo systemctl stop tradingbot; pkill -f range_harvester.py; sudo systemctl start tradingbot`")
                await asyncio.sleep(min(5 * conflicts, 60))
                continue
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


def claim_single_instance():
    """Only one process may poll Telegram. Two pollers on one token means getUpdates
    returns 409 and the bot silently stops receiving commands while still able to send —
    which looks exactly like 'it just stopped'. An old process lingering after a restart
    is the usual cause, so refuse to start rather than become the second one."""
    import fcntl
    lock_path = os.path.join(BASE_DIR, "bot.lock")
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another instance already holds the lock; exiting so it keeps the token")
        raise SystemExit(0)
    handle.write(str(os.getpid()))
    handle.flush()
    return handle          # held for the life of the process


async def main():
    global bot
    _lock = claim_single_instance()
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
            BotCommand("check", "فحص الاتصال والحساب"),
            BotCommand("live", "تنفيذ آلي على MetaApi (البوت ينفّذ بنفسه)"),
            BotCommand("gold", "ضبط اسم أداة الذهب عند وسيطك تلقائياً"),
            BotCommand("hook", "ربط TradingView: الرابط والحالة"),
            BotCommand("signal", "وضع الإشارات: أنت تنفّذ بـ MT5"),
            BotCommand("lot", "تغيير حجم اللوت: /lot 0.01"),
            BotCommand("entry", "تصحيح سعر الدخول: /entry 4468.5"),
            BotCommand("paper", "حساب تجريبي داخلي بدون وسيط"),
            BotCommand("report", "أداء الحساب التجريبي"),
            BotCommand("capital", "ربط Capital.com: /capital <مفتاح> <إيميل> <كلمة سر>"),
            BotCommand("setkey", "حفظ مفتاح: /setkey claude sk-ant-..."),
            BotCommand("update", "تحديث الكود من GitHub"),
            BotCommand("restart", "إعادة تشغيل البوت"),
            BotCommand("help", "قائمة الأوامر"),
        ])
    except Exception as e:
        log.warning(f"set_my_commands failed: {e}")
    global HOOK_SECRET
    if HOOK_ENABLED and not HOOK_SECRET:
        import secrets
        HOOK_SECRET = secrets.token_urlsafe(24)
        write_env_value("HOOK_SECRET", HOOK_SECRET)
        log.info("generated a webhook secret")
    log.info("Bot booting up (supervised mode)")
    tasks = [
        supervise("connection", ensure_connection),
        supervise("range_engine", range_engine),
        supervise("telegram_listener", telegram_listener),
        supervise("cost_governor", cost_governor),
        supervise("hook_server", hook_server),
    ]
    if intel is not None and not LEAN:
        tasks.append(supervise("news_watcher", lambda: intel.news_watcher(fetch_candles, SYMBOL, notify)))
        tasks.append(supervise("daily_scheduler", lambda: intel.daily_scheduler(fetch_candles, SYMBOL, notify)))
        log.info(f"broker={BROKER} | intel enabled: claude={'yes' if intel.claude_ready() else 'no'} news={'yes' if intel.FINNHUB_API_KEY else 'no'}")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
