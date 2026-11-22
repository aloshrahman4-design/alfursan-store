"""A conditional order the BROKER holds, not the bot.

The owner's request: "when price reaches X, open a buy, and close it at profit Y."

The obvious implementation is a loop that watches the price and fires a market
order when it arrives. That implementation is wrong here and this whole project
is the evidence. The bot sees price about a second and a half late, it died on
this account for a day and a half without anyone knowing, and the one protection
that has never failed is the one the broker holds. A watcher would turn "open at
3850" into "open at whatever 3850 became while I was waking up", and into nothing
at all on a night the process is down.

So /order places a real pending order at the broker, with its take profit and its
stop attached to it. The broker fills it at the level, closes it at the target,
and stops it at the stop - with the bot off, the VM rebooting, or MetaApi
unreachable. The bot's only jobs are to get the arithmetic right, to refuse an
order the broker would reject, and to show the owner exactly what he is about to
sign before anything is sent.

Which pending type is a decision, not a preference: a buy BELOW the market is a
limit, a buy ABOVE it is a stop, and the two are inverted for a sell. Getting it
backwards is an order that either never fills or fills instantly at the wrong
price, so it is computed here and asserted in the tests.

After it fills, the trade belongs to the broker's own target and stop. The bot
does not trail it and does not close it: the owner asked for an exit at a price,
and a give-back rule that quietly closed it earlier would be the same disease
this project spent weeks curing - a rule silently replacing the one he asked for.
/adopt hands it to the bot afterwards, when he wants that.
"""

MAGIC_ORDER = 777777


def kind(side, price, bid, ask):
    """Which pending order this is, or (None, why).

    A buy is filled at the ask and a sell at the bid, so each side is measured
    against the price it would actually transact at.
    """
    side = str(side or "").strip().lower()
    try:
        price, bid, ask = float(price), float(bid), float(ask)
    except (TypeError, ValueError):
        return None, "السعر مو رقم"
    if side not in ("buy", "sell"):
        return None, "الاتجاه لازم `buy` أو `sell`"
    if price <= 0 or bid <= 0 or ask <= 0:
        return None, "سعر غير صالح"
    if side == "buy":
        if price < ask:
            return "buy_limit", ""        # waiting for a dip
        if price > ask:
            return "buy_stop", ""         # waiting for a breakout
        return None, "سعر الشراء يساوي السوق — استخدم صفقة فورية مو أمر معلّق"
    if price > bid:
        return "sell_limit", ""           # waiting for a rally to sell into
    if price < bid:
        return "sell_stop", ""            # waiting for a breakdown
    return None, "سعر البيع يساوي السوق — استخدم صفقة فورية مو أمر معلّق"


def check(side, price, tp, sl, bid, ask, gap=0.0, lot=0.01, contract=100.0):
    """Everything about the order, or (None, why) with the reason in plain words.

    Refuses here whatever the broker would refuse there. A rejection from MetaApi
    arrives as a code the owner cannot read, minutes after he typed the command;
    a refusal here arrives with the number that was wrong.
    """
    k, why = kind(side, price, bid, ask)
    if k is None:
        return None, why
    buy = k.startswith("buy")
    price = float(price)
    gap = max(0.0, float(gap or 0.0))

    # The entry itself has to clear the broker's minimum distance from the market.
    ref = float(ask) if buy else float(bid)
    if gap and abs(price - ref) < gap:
        return None, (f"السعر `{price}` قريب من السوق `{round(ref, 2)}` — "
                      f"الوسيط يطلب `{gap}$` على الأقل. ابعده شوي.")

    tp = None if tp in (None, "") else float(tp)
    sl = None if sl in (None, "") else float(sl)

    if tp is not None:
        if buy and tp <= price:
            return None, f"هدف الشراء `{tp}` لازم يكون **فوق** الدخول `{price}`"
        if not buy and tp >= price:
            return None, f"هدف البيع `{tp}` لازم يكون **تحت** الدخول `{price}`"
        if gap and abs(tp - price) < gap:
            return None, (f"الهدف `{tp}` قريب من الدخول — "
                          f"الوسيط يطلب `{gap}$` على الأقل")
    if sl is not None:
        if buy and sl >= price:
            return None, f"وقف الشراء `{sl}` لازم يكون **تحت** الدخول `{price}`"
        if not buy and sl <= price:
            return None, f"وقف البيع `{sl}` لازم يكون **فوق** الدخول `{price}`"
        if gap and abs(price - sl) < gap:
            return None, (f"الوقف `{sl}` قريب من الدخول — "
                          f"الوسيط يطلب `{gap}$` على الأقل")

    per = float(lot) * float(contract)
    risk = round(abs(price - sl) * per, 2) if sl is not None else None
    reward = round(abs(tp - price) * per, 2) if tp is not None else None
    rr = round(reward / risk, 2) if (risk and reward) else None
    return {
        "kind": k, "side": "buy" if buy else "sell", "price": round(price, 2),
        "tp": round(tp, 2) if tp is not None else None,
        "sl": round(sl, 2) if sl is not None else None,
        "lot": float(lot), "risk": risk, "reward": reward, "rr": rr,
        "ref": round(ref, 2),
    }, ""


KIND_WORD = {"buy_limit": "شراء عند هبوط (Buy Limit)",
             "buy_stop": "شراء عند اختراق (Buy Stop)",
             "sell_limit": "بيع عند صعود (Sell Limit)",
             "sell_stop": "بيع عند كسر (Sell Stop)"}


def plan_text(p, symbol):
    """What he is about to sign, in full, before anything is sent."""
    lines = [f"📋 *أمر معلّق على `{symbol}`*",
             f"• النوع: {KIND_WORD.get(p['kind'], p['kind'])}",
             f"• ينفّذ عند `{p['price']}` (السوق الآن `{p['ref']}`)",
             f"• اللوت `{p['lot']:g}`"]
    if p["tp"] is not None:
        lines.append(f"• 🎯 يغلق على ربح عند `{p['tp']}` = `{p['reward']}$`")
    else:
        lines.append("• ⚠️ ماكو هدف — يبقى مفتوح لين تسكّره بنفسك")
    if p["sl"] is not None:
        lines.append(f"• 🛡 وقف عند `{p['sl']}` = خسارة `{p['risk']}$`")
    else:
        lines.append("• 🚨 *ماكو وقف* — الخسارة بلا حد. أضف `sl <سعر>`")
    if p["rr"] is not None:
        verdict = "✅" if p["rr"] >= 1.5 else "⚠️" if p["rr"] >= 1 else "⛔️"
        lines.append(f"• الربح مقابل الخسارة `{p['rr']}` {verdict}")
    lines += ["",
              "الوسيط هو اللي ينفّذه ويغلقه — يشتغل حتى لو البوت مطفي أو النت مقطوع.",
              "وبعد ما ينفتح، البوت **ما يديره**؛ `/adopt` يسلّمه له إذا تريد.",
              "", "`/order yes` ينفّذ · `/order no` يلغي"]
    return "\n".join(lines)


def maker_name(k):
    """The connection method that places this kind."""
    return {"buy_limit": "create_limit_buy_order",
            "buy_stop": "create_stop_buy_order",
            "sell_limit": "create_limit_sell_order",
            "sell_stop": "create_stop_sell_order"}[k]
