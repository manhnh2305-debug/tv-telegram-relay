import os, json, time, requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")
ICT       = timezone(timedelta(hours=7))

ZONE_TTL  = 4 * 3600   # 4 tiếng
RR        = 2.0         # Risk:Reward 1:2

zone_state = {}   # { "XAUUSD": { active, expires, type, top, bottom } }
pending    = {}   # { callback_id: signal_data }

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def now_ict():
    return datetime.now(ICT).strftime("%H:%M ICT %d/%m")

def send_text(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)

def calc_sl_tp(action, price, zone_top, zone_bottom):
    """Tính SL/TP với RR 1:2"""
    try:
        price = float(price)
        if action == "SELL" and zone_top > 0:
            sl   = zone_top
            risk = sl - price
            tp   = round(price - risk * RR, 2)
            return round(sl, 2), tp
        elif action == "BUY" and zone_bottom > 0:
            sl   = zone_bottom
            risk = price - sl
            tp   = round(price + risk * RR, 2)
            return round(sl, 2), tp
    except Exception:
        pass
    return None, None

def send_signal_message(data, callback_id):
    symbol = data.get("symbol", "XAUUSD")
    price  = data.get("price",  "—")
    action = data.get("action", "—")
    tf     = data.get("tf",     "M5")
    harsi  = data.get("harsi",  "—")

    # Lấy zone info để tính SL/TP
    zs         = zone_state.get(symbol, {})
    zone_top   = zs.get("top",    0)
    zone_bottom= zs.get("bottom", 0)
    sl, tp     = calc_sl_tp(action, price, zone_top, zone_bottom)

    emoji    = "🔴" if action == "SELL" else "🟢"
    tf_emoji = "⚡" if tf == "M5" else "🔔"
    header   = f"{tf_emoji} <b>SIGNAL {action} — {symbol} {tf}</b>" if tf == "M5" else \
               f"{tf_emoji} <b>M15 CONFIRM {action} — {symbol}</b>"
    footer   = "👉 Xác nhận vào lệnh:" if tf == "M5" else "💡 M15 đủ điều kiện — xem xét add thêm:"
    interval = "5" if tf == "M5" else "15"
    chart    = f"https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD&interval={interval}"

    sl_line = f"🛑 SL: <b>{sl}</b>\n" if sl else ""
    tp_line = f"🎯 TP: <b>{tp}</b>  (RR 1:{int(RR)})\n" if tp else ""

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📍 Giá: <b>{price}</b>\n"
        f"📊 HARSI: {harsi}\n"
        f"{sl_line}"
        f"{tp_line}"
        f"⏰ {now_ict()}\n"
        f"🔗 <a href='{chart}'>Mở chart {tf}</a>\n\n"
        f"{footer}"
    )

    # Lưu SL/TP vào pending
    data["sl"] = sl
    data["tp"] = tp

    keyboard = {"inline_keyboard": [[
        {"text": f"{emoji} {action}", "callback_data": f"confirm_{callback_id}_{action}"},
        {"text": "❌ Bỏ qua",         "callback_data": f"skip_{callback_id}"}
    ]]}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": CHAT_ID, "text": text,
        "parse_mode": "HTML", "reply_markup": keyboard
    }, timeout=10)

def answer_callback(cq_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": cq_id, "text": text}, timeout=5)

def edit_message(chat_id, msg_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    requests.post(url, json={
        "chat_id": chat_id, "message_id": msg_id,
        "text": text, "parse_mode": "HTML"
    }, timeout=5)

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/alert", methods=["POST"])
def alert():
    """
    Nhận rectangle alert từ TradingView.
    Format: XAUUSD|{{close}}|RESISTANCE|4550|4530|H1|Ghi chú
    """
    data = {}
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        pass

    if not data:
        raw   = request.data.decode("utf-8").strip()
        parts = [p.strip() for p in raw.split("|")]
        keys  = ["symbol", "price", "zone_type", "top", "bottom", "timeframe", "note"]
        data  = dict(zip(keys, parts))

    symbol    = data.get("symbol",    "XAUUSD")
    price     = data.get("price",     "—")
    zone_type = data.get("zone_type", "ZONE").upper()
    note      = data.get("note",      "")

    try:
        zone_top    = float(data.get("top",    0))
        zone_bottom = float(data.get("bottom", 0))
    except Exception:
        zone_top = zone_bottom = 0.0

    zone_state[symbol] = {
        "active":  True,
        "expires": time.time() + ZONE_TTL,
        "type":    zone_type,
        "top":     zone_top,
        "bottom":  zone_bottom,
    }

    emoji = "🔴" if zone_type == "RESISTANCE" else "🟢"
    sl_info = f"\n📐 Box: {zone_bottom} — {zone_top}" if zone_top and zone_bottom else ""

    text = (
        f"{emoji} <b>{symbol}</b> — Giá vào vùng <b>{zone_type}</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📍 Giá: <b>{price}</b>"
        f"{sl_info}\n"
        f"⏰ {now_ict()}\n"
        f"{'📝 ' + note + chr(10) if note else ''}"
        f"\n👀 <i>Đang theo dõi setup M5 / M15...</i>"
    )
    send_text(text)
    return jsonify({"status": "zone_active", "symbol": symbol}), 200


@app.route("/signal", methods=["POST"])
def signal():
    """Nhận tín hiệu từ Pine Script — chỉ forward nếu zone active."""
    data = {}
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        pass

    if not data:
        raw = request.data.decode("utf-8").strip()
        try:
            data = json.loads(raw)
        except Exception:
            return jsonify({"error": "invalid body"}), 400

    symbol = data.get("symbol", "XAUUSD")
    zs     = zone_state.get(symbol)

    if not zs or not zs.get("active") or time.time() > zs.get("expires", 0):
        return jsonify({"status": "ignored", "reason": "zone_not_active"}), 200

    callback_id      = str(int(time.time()))
    pending[callback_id] = data
    send_signal_message(data, callback_id)
    return jsonify({"status": "sent", "callback_id": callback_id}), 200


@app.route("/webhook_tg", methods=["POST"])
def webhook_tg():
    """Nhận callback từ Telegram khi bấm nút inline."""
    update = request.get_json(force=True, silent=True) or {}
    cq     = update.get("callback_query")
    if not cq:
        return jsonify({"ok": True}), 200

    cq_id   = cq["id"]
    cq_data = cq.get("data", "")
    message = cq.get("message", {})
    msg_id  = message.get("message_id")
    chat_id = message.get("chat", {}).get("id")
    parts   = cq_data.split("_")

    if parts[0] == "confirm" and len(parts) >= 3:
        cb_id  = parts[1]
        action = parts[2]
        sig    = pending.pop(cb_id, {})
        symbol = sig.get("symbol", "XAUUSD")
        price  = sig.get("price",  "—")
        tf     = sig.get("tf",     "M5")
        sl     = sig.get("sl",     "—")
        tp     = sig.get("tp",     "—")
        emoji  = "🔴" if action == "SELL" else "🟢"

        answer_callback(cq_id, f"✅ Đã xác nhận {action}!")
        edit_message(chat_id, msg_id,
            f"{emoji} <b>ĐÃ XÁC NHẬN {action}</b> — {symbol} {tf}\n"
            f"📍 Entry: {price}\n"
            f"🛑 SL: {sl}\n"
            f"🎯 TP: {tp}  (RR 1:{int(RR)})\n"
            f"⏰ {now_ict()}\n"
            f"⏳ <i>Đang gửi lệnh xuống MT5...</i>"
        )
        # TODO GĐ3: gửi lệnh xuống MT5 EA

    elif parts[0] == "skip" and len(parts) >= 2:
        cb_id = parts[1]
        sig   = pending.pop(cb_id, {})
        answer_callback(cq_id, "❌ Đã bỏ qua")
        edit_message(chat_id, msg_id,
            f"❌ <b>Đã bỏ qua</b> — {sig.get('symbol','XAUUSD')} {sig.get('tf','')}\n"
            f"⏰ {now_ict()}"
        )

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
