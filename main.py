import os, json, time, requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")
ICT       = timezone(timedelta(hours=7))

# ── Zone flag state ────────────────────────────────────────────────────────────
# Khi giá vào vùng cản (rectangle alert) → bật flag này
# Tự tắt sau ZONE_TTL giây
ZONE_TTL      = 4 * 3600   # 4 tiếng
zone_state    = {}          # { "XAUUSD": { "active": True, "expires": timestamp, "type": "RESISTANCE" } }

# ── Pending signals (chờ xác nhận) ────────────────────────────────────────────
pending = {}   # { "callback_id": { signal_data } }

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def now_ict():
    return datetime.now(ICT).strftime("%H:%M ICT %d/%m")

def send_text(text):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    return requests.post(url, json=payload, timeout=10)

def send_signal_message(data, callback_id):
    """Gửi tin nhắn tín hiệu kèm nút xác nhận inline."""
    symbol = data.get("symbol", "XAUUSD")
    price  = data.get("price",  "—")
    action = data.get("action", "—")
    tf     = data.get("tf",     "M5")
    harsi  = data.get("harsi",  "—")

    emoji    = "🔴" if action == "SELL" else "🟢"
    tf_emoji = "⚡" if tf == "M5" else "🔔"

    if tf == "M5":
        header = f"{tf_emoji} <b>SIGNAL {action} — {symbol} M5</b>"
        footer = "👉 Xác nhận vào lệnh:"
    else:
        header = f"{tf_emoji} <b>M15 CONFIRM {action} — {symbol}</b>"
        footer = "💡 M15 đủ điều kiện — xem xét add thêm:"

    chart_url = f"https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD&interval={'5' if tf == 'M5' else '15'}"

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📍 Giá: <b>{price}</b>\n"
        f"📊 HARSI: {harsi}\n"
        f"⏰ {now_ict()}\n"
        f"🔗 <a href='{chart_url}'>Mở chart {tf}</a>\n\n"
        f"{footer}"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": f"{emoji} {action}", "callback_data": f"confirm_{callback_id}_{action}"},
            {"text": "❌ Bỏ qua",         "callback_data": f"skip_{callback_id}"}
        ]]
    }

    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":      CHAT_ID,
        "text":         text,
        "parse_mode":   "HTML",
        "reply_markup": keyboard
    }
    return requests.post(url, json=payload, timeout=10)

def answer_callback(callback_query_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=5)

def edit_message_reply_markup(chat_id, message_id, text):
    """Xóa nút sau khi bấm."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    requests.post(url, json={
        "chat_id": chat_id, "message_id": message_id,
        "text": text, "parse_mode": "HTML"
    }, timeout=5)

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/alert", methods=["POST"])
def alert():
    """
    Nhận alert vùng cản H1 từ TradingView (rectangle alert).
    Format: XAUUSD|{{close}}|RESISTANCE|H1|Tên vùng
    """
    data = {}
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        pass

    if not data:
        raw   = request.data.decode("utf-8").strip()
        parts = [p.strip() for p in raw.split("|")]
        keys  = ["symbol", "price", "zone_type", "timeframe", "note"]
        data  = dict(zip(keys, parts))

    symbol    = data.get("symbol",    "XAUUSD")
    price     = data.get("price",     "—")
    zone_type = data.get("zone_type", "ZONE").upper()
    note      = data.get("note",      "")

    # Bật zone flag
    zone_state[symbol] = {
        "active":  True,
        "expires": time.time() + ZONE_TTL,
        "type":    zone_type
    }

    emoji = "🔴" if zone_type == "RESISTANCE" else "🟢"
    text  = (
        f"{emoji} <b>{symbol}</b> — Giá vào vùng <b>{zone_type}</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📍 Giá: <b>{price}</b>\n"
        f"⏰ {now_ict()}\n"
        f"{'📝 ' + note + chr(10) if note else ''}"
        f"\n👀 <i>Đang theo dõi setup M5 / M15...</i>"
    )
    send_text(text)
    return jsonify({"status": "zone_active", "symbol": symbol}), 200


@app.route("/signal", methods=["POST"])
def signal():
    """
    Nhận tín hiệu entry từ Pine Script indicator.
    Chỉ forward nếu zone flag đang active.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    if not data:
        raw  = request.data.decode("utf-8").strip()
        try:
            data = json.loads(raw)
        except Exception:
            return jsonify({"error": "invalid body"}), 400

    symbol = data.get("symbol", "XAUUSD")

    # Kiểm tra zone flag
    zs = zone_state.get(symbol)
    if not zs or not zs.get("active") or time.time() > zs.get("expires", 0):
        # Zone không active — bỏ qua, không gửi Telegram
        return jsonify({"status": "ignored", "reason": "zone_not_active"}), 200

    # Tạo callback ID duy nhất
    callback_id = str(int(time.time()))
    pending[callback_id] = data

    send_signal_message(data, callback_id)
    return jsonify({"status": "sent", "callback_id": callback_id}), 200


@app.route("/webhook_tg", methods=["POST"])
def webhook_tg():
    """
    Nhận callback từ Telegram khi bấm nút inline.
    Set webhook này trên Telegram để nhận callback_query.
    """
    update = request.get_json(force=True, silent=True) or {}

    callback_query = update.get("callback_query")
    if not callback_query:
        return jsonify({"ok": True}), 200

    cq_id      = callback_query["id"]
    cq_data    = callback_query.get("data", "")
    message    = callback_query.get("message", {})
    msg_id     = message.get("message_id")
    chat_id    = message.get("chat", {}).get("id")
    parts      = cq_data.split("_")   # confirm_<id>_<action> hoặc skip_<id>

    if parts[0] == "confirm" and len(parts) >= 3:
        cb_id   = parts[1]
        action  = parts[2]
        sig     = pending.pop(cb_id, {})
        symbol  = sig.get("symbol", "XAUUSD")
        price   = sig.get("price", "—")
        tf      = sig.get("tf", "M5")
        emoji   = "🔴" if action == "SELL" else "🟢"

        answer_callback(cq_id, f"✅ Đã xác nhận {action}!")
        edit_message_reply_markup(chat_id, msg_id,
            f"{emoji} <b>ĐÃ XÁC NHẬN {action}</b> — {symbol}\n"
            f"📍 Giá: {price} | {tf} | {now_ict()}\n"
            f"⏳ <i>Đang gửi lệnh xuống MT5...</i>"
        )
        # TODO: gửi lệnh xuống MT5 EA (Giai đoạn 3)

    elif parts[0] == "skip" and len(parts) >= 2:
        cb_id = parts[1]
        sig   = pending.pop(cb_id, {})
        answer_callback(cq_id, "❌ Đã bỏ qua tín hiệu")
        edit_message_reply_markup(chat_id, msg_id,
            f"❌ <b>Đã bỏ qua</b> — {sig.get('symbol','XAUUSD')} {sig.get('tf','')}\n"
            f"⏰ {now_ict()}"
        )

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
