"""
relay_server.py v4.4.0 — PATCH NOTES
═══════════════════════════════════════
Thay doi tu v4.3.1:
  ✅ v4.4.0: 3 muc TP (TP1=1R, TP2=2R, TP3=3R) thay vi 2 muc
  ✅ v4.4.0: Pivot SL - parse pivot_high/pivot_low tu Pine alert
  ✅ v4.4.0: Weekly stats - in-memory log + /weekly_report endpoint
  ✅ v4.4.0: /trigger_weekly - UptimeRobot goi Chu nhat toi

AP DUNG: Tim cac doan code ORIGINAL va thay bang PATCHED
"""

# ═══════════════════════════════════════════════════════════════════
# PATCH 1: Them bien weekly_signals (sau seen_signals = {})
# ═══════════════════════════════════════════════════════════════════

# --- ORIGINAL (dong ~40, sau seen_signals = {}) ---
# last_signal_log = {}
# startup_time    = time.time()
# request_count   = {"signal": 0, "alert": 0, "mt5_poll": 0}

# --- PATCHED ---
"""
last_signal_log = {}
startup_time    = time.time()
request_count   = {"signal": 0, "alert": 0, "mt5_poll": 0}

# v4.4.0: Weekly signal log
weekly_signals = []  # list of dicts: {time, symbol, action, price, sl, tp1, tp2, tp3, tf, channel_type}
"""


# ═══════════════════════════════════════════════════════════════════
# PATCH 2: Thay format_channel_message() — 3 TPs
# ═══════════════════════════════════════════════════════════════════

# --- THAY TOAN BO ham format_channel_message ---
"""
def format_channel_message(sig, channel_type):
    action   = sig.get("action", "")
    symbol   = sig.get("symbol", "XAUUSD").replace(SYMBOL_SUFFIX, "")
    price    = sig.get("price", "—")
    sl       = sig.get("sl", "—")
    tf       = sig.get("tf", "M5")
    sig_type = sig.get("signal_type", "vsa")
    trend    = sig.get("trend", "")
    mode     = sig.get("mode", "")
    digits   = get_digits(symbol)

    # v4.4.0: 3 muc TP
    tp1 = tp2 = tp3 = "—"
    try:
        p, s = float(price), float(sl)
        risk_dist = abs(p - s)
        if risk_dist > 0:
            if action == "BUY":
                tp1 = f"{p + risk_dist:.{digits}f}"
                tp2 = f"{p + risk_dist * 2:.{digits}f}"
                tp3 = f"{p + risk_dist * 3:.{digits}f}"
            else:
                tp1 = f"{p - risk_dist:.{digits}f}"
                tp2 = f"{p - risk_dist * 2:.{digits}f}"
                tp3 = f"{p - risk_dist * 3:.{digits}f}"
    except (ValueError, TypeError, ZeroDivisionError):
        pass

    reason = "HARSI divergence + VSA confirmation" if sig_type == "div" else "VSA engulfing + volume shift"
    if trend: reason += f" | Trend: {trend}"
    if mode:  reason += f" ({mode})"

    arrow, emoji, direction = ("🚀", "🟢", "BUY") if action == "BUY" else ("📉", "🔴", "SELL")
    if tf in ("M5", "M15", "M30"):
        tf_tag = "⚡ SCALP"
    elif tf in ("H1", "H4", "SWING"):
        tf_tag = "📊 SWING"
    else:
        tf_tag = "📈 POSITION"
    channel_icon = {"xau": "🥇", "forex": "💱", "crypto": "🪙"}.get(channel_type, "📊")

    return (
        f"{channel_icon} {arrow} <b>{emoji} {direction} {symbol}</b>\\n"
        f"━━━━━━━━━━━━━━━━━━━━\\n"
        f"{tf_tag} | {tf}\\n\\n"
        f"📍 Entry: <b>{price}</b>\\n"
        f"🛡 SL: <b>{sl}</b>\\n"
        f"🎯 TP1: <b>{tp1}</b>  — RR 1:1\\n"
        f"🏆 TP2: <b>{tp2}</b>  — RR 1:2\\n"
        f"💎 TP3: <b>{tp3}</b>  — RR 1:3\\n"
        f"━━━━━━━━━━━━━━━━━━━━\\n"
        f"💡 <i>{reason}</i>\\n"
        f"🕐 {now_ict()}\\n\\n"
        f"#signal #{symbol} #{direction.lower()}\\n"
        f"🔔 <b>Đừng Đu Đỉnh</b>"
    )
"""


# ═══════════════════════════════════════════════════════════════════
# PATCH 3: Trong signal_route() — parse pivot + log weekly
# Tim doan "# SL/TP calculation" va thay
# ═══════════════════════════════════════════════════════════════════

# --- ORIGINAL (trong signal_route, phan SL/TP) ---
#         div_sl = data.get("div_sl")
#         if div_sl and (sig_type == "div" or bypass_zone):

# --- PATCHED (thay toan bo block SL/TP) ---
"""
        # v4.4.0: Parse pivot data tu Pine
        pivot_high = data.get("pivot_high")
        pivot_low = data.get("pivot_low")

        # SL/TP calculation - uu tien pivot, fallback div_sl, fallback zone
        div_sl = data.get("div_sl")

        if div_sl and (sig_type == "div" or bypass_zone):
            try:
                sl = round(float(div_sl), digits)
                price_f = float(price)
                risk = abs(price_f - sl)
                tp = round(price_f + risk * DEFAULT_RR, digits) if action == "BUY" else round(price_f - risk * DEFAULT_RR, digits)
            except Exception:
                sl, tp = None, None
        else:
            sl, tp = calc_sl_tp(action, price,
                                zs.get("top", 0) if zs else 0,
                                zs.get("bottom", 0) if zs else 0, symbol)

        data["sl"] = sl
        data["tp"] = tp
"""

# === SAU KHI route_to_channels(crypto_copy) thanh cong, them log: ===
# (Them vao cuoi signal_route, TRUOC return)
"""
    # v4.4.0: Log signal cho weekly report
    try:
        p_f, s_f = float(price), float(sl) if sl else 0
        risk_d = abs(p_f - s_f) if s_f else 0
        tp1_v = (p_f + risk_d) if action == "BUY" else (p_f - risk_d) if risk_d else 0
        tp2_v = (p_f + risk_d*2) if action == "BUY" else (p_f - risk_d*2) if risk_d else 0
        tp3_v = (p_f + risk_d*3) if action == "BUY" else (p_f - risk_d*3) if risk_d else 0
    except: tp1_v = tp2_v = tp3_v = 0

    weekly_signals.append({
        "time": now_ict_full(), "symbol": symbol, "action": action,
        "price": price, "sl": sl, "tp1": round(tp1_v, digits) if tp1_v else 0,
        "tp2": round(tp2_v, digits) if tp2_v else 0,
        "tp3": round(tp3_v, digits) if tp3_v else 0,
        "tf": tf, "channel_type": classify_symbol(symbol),
        "signal_type": sig_type,
    })
"""


# ═══════════════════════════════════════════════════════════════════
# PATCH 4: Them legacy post 3 TPs (thay send_public_signal_legacy)
# ═══════════════════════════════════════════════════════════════════

# Trong send_public_signal_legacy, thay TP section tuong tu format_channel_message
# TP1 = 1R, TP2 = 2R, TP3 = 3R (thay vi chi TP1 + TP2 nhu truoc)


# ═══════════════════════════════════════════════════════════════════
# PATCH 5: Them route /weekly_report va /trigger_weekly
# Them CUOI FILE, truoc if __name__ == "__main__":
# ═══════════════════════════════════════════════════════════════════

"""
# ═════════════════════════════════════════════════════════════════════════════
# WEEKLY REPORT (v4.4.0)
# ═════════════════════════════════════════════════════════════════════════════

def build_weekly_report():
    if not weekly_signals:
        return None

    now = datetime.now(ICT)
    week_start = (now - timedelta(days=now.weekday())).strftime("%d/%m")
    week_end = now.strftime("%d/%m/%Y")

    # Thong ke
    total = len(weekly_signals)
    buys = sum(1 for s in weekly_signals if s["action"] == "BUY")
    sells = total - buys

    by_channel = {}
    for s in weekly_signals:
        ct = s.get("channel_type", "unknown")
        by_channel.setdefault(ct, {"total": 0, "buy": 0, "sell": 0})
        by_channel[ct]["total"] += 1
        if s["action"] == "BUY": by_channel[ct]["buy"] += 1
        else: by_channel[ct]["sell"] += 1

    by_symbol = {}
    for s in weekly_signals:
        sym = s.get("symbol", "?")
        by_symbol[sym] = by_symbol.get(sym, 0) + 1

    # Top symbols
    top_syms = sorted(by_symbol.items(), key=lambda x: x[1], reverse=True)[:5]

    report = (
        f"📊 <b>THONG KE TUAN {week_start} — {week_end}</b>\\n"
        f"━━━━━━━━━━━━━━━━━━━━\\n"
        f"📡 Tong: <b>{total}</b> tin hieu\\n"
        f"🟢 BUY: {buys} | 🔴 SELL: {sells}\\n"
        f"━━━━━━━━━━━━━━━━━━━━\\n"
    )

    channel_names = {"xau": "🥇 XAU", "forex": "💱 Forex", "crypto": "🪙 Crypto"}
    for ct, stats in by_channel.items():
        name = channel_names.get(ct, ct)
        report += f"{name}: {stats['total']} ({stats['buy']}B / {stats['sell']}S)\\n"

    if top_syms:
        report += f"\\n📈 <b>Top symbols:</b>\\n"
        for sym, count in top_syms:
            report += f"  {sym}: {count} tin hieu\\n"

    # List 10 tin hieu gan nhat
    report += f"\\n━━━━━━━━━━━━━━━━━━━━\\n"
    report += f"🕐 <b>10 tin hieu gan nhat:</b>\\n"
    for s in weekly_signals[-10:]:
        emoji = "🟢" if s["action"] == "BUY" else "🔴"
        report += f"{emoji} {s['action']} {s['symbol']} @ {s['price']} | SL:{s.get('sl','—')} | {s['tf']}\\n"

    report += (
        f"\\n━━━━━━━━━━━━━━━━━━━━\\n"
        f"🔔 <b>Đừng Đu Đỉnh</b> — Bao cao tu dong"
    )
    return report


@app.route("/weekly_report", methods=["GET", "POST"])
def weekly_report():
    if not verify_api_key(): return jsonify({"error": "unauthorized"}), 401

    report = build_weekly_report()
    if not report:
        return jsonify({"status": "no_signals", "message": "Khong co tin hieu tuan nay"}), 200

    # Gui vao tat ca channels da cau hinh
    sent = []
    for channel_type, channel_id in [("xau", XAU_CHANNEL_ID), ("forex", FOREX_CHANNEL_ID), ("crypto", CRYPTO_CHANNEL_ID)]:
        if channel_id:
            ok = send_to_channel(channel_id, report)
            if ok:
                sent.append(channel_type)
                log.info(f"📊 Weekly report → {channel_type} ({channel_id})")

    # Gui vao Signal Bot (private)
    send_text(f"📊 <b>Weekly Report da gui</b>\\nChannels: {', '.join(sent) if sent else 'none'}")

    return jsonify({"status": "sent", "channels": sent, "total_signals": len(weekly_signals)}), 200


@app.route("/trigger_weekly", methods=["GET", "POST"])
def trigger_weekly():
    \"\"\"
    UptimeRobot goi endpoint nay moi Chu nhat toi.
    Khong can API key — chi gui report va reset.
    \"\"\"
    now = datetime.now(ICT)
    # Chi chay vao Chu nhat (weekday=6)
    if now.weekday() != 6:
        return jsonify({"status": "not_sunday", "day": now.strftime("%A")}), 200

    report = build_weekly_report()
    if not report:
        return jsonify({"status": "no_signals"}), 200

    sent = []
    for channel_type, channel_id in [("xau", XAU_CHANNEL_ID), ("forex", FOREX_CHANNEL_ID), ("crypto", CRYPTO_CHANNEL_ID)]:
        if channel_id:
            ok = send_to_channel(channel_id, report)
            if ok: sent.append(channel_type)

    send_text(f"📊 <b>Auto Weekly Report</b>\\n{', '.join(sent)}")

    # Reset weekly log
    global weekly_signals
    weekly_signals = []

    return jsonify({"status": "sent", "channels": sent}), 200
"""


# ═══════════════════════════════════════════════════════════════════
# PATCH 6: Update health endpoint — them weekly_signals count
# ═══════════════════════════════════════════════════════════════════

# Trong health() route, them vao response:
# "weekly_signals": len(weekly_signals),


# ═══════════════════════════════════════════════════════════════════
# PATCH 7: Update version string
# ═══════════════════════════════════════════════════════════════════
# Thay "4.3.1" -> "4.4.0" o tat ca cac cho (health, status, notify_startup, docstring)
