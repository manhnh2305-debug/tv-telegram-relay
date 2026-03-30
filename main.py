import os, json, time, uuid, requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")
ICT       = timezone(timedelta(hours=7))

ZONE_TTL  = 4 * 3600
zone_state = {}
pending    = {}      # chờ xác nhận Telegram
mt5_queue  = {}      # chờ EA poll
risk_state = {}      # { chat_id: { callback_id, waiting_risk } }

def now_ict():
    return datetime.now(ICT).strftime("%H:%M ICT %d/%m")

def send_text(text, reply_markup=None):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return requests.post(url, json=payload, timeout=10)

def edit_message(chat_id, msg_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    requests.post(url, json={
        "chat_id": chat_id, "message_id": msg_id,
        "text": text, "parse_mode": "HTML"
    }, timeout=5)

def answer_callback(cq_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": cq_id, "text": text}, timeout=5)

def calc_sl_tp(action, price, zone_top, zone_bottom, rr=2.0):
    try:
        price = float(price)
        if action == "SELL" and zone_top > 0:
            sl   = zone_top
            risk = sl - price
            tp   = round(price - risk * rr, 2)
            return round(sl, 2), tp
        elif action == "BUY" and zone_bottom > 0:
            sl   = zone_bottom
            risk = price - sl
            tp   = round(price + risk * rr, 2)
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

    zs          = zone_state.get(symbol, {})
    zone_top    = zs.get("top",    0)
    zone_bottom = zs.get("bottom", 0)
    sl, tp      = calc_sl_tp(action, price, zone_top, zone_bottom)

    data["sl"] = sl
    data["tp"] = tp

    emoji    = "🔴" if action == "SELL" else "🟢"
    tf_emoji = "⚡" if tf == "M5" else "🔔"
    interval = "5" if tf == "M5" else "15"
    chart    = f"https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD&interval={interval}"
    header   = f"{tf_emoji} <b>SIGNAL {action} — {symbol} {tf}</b>"

    sl_line = f"🛑 SL: <b>{sl}</b>\n" if sl else ""
    tp_line = f"🎯 TP: <b>{tp}</b>  (RR 1:2)\n" if tp else ""

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📍 Giá: <b>{price}</b>\n"
        f"📊 HARSI: {harsi}\n"
        f"{sl_line}{tp_line}"
        f"⏰ {now_ict()}\n"
        f"🔗 <a href='{chart}'>Mở chart {tf}</a>\n\n"
        f"👉 Xác nhận vào lệnh:"
    )

    keyboard = {"inline_keyboard": [[
        {"text": f"{emoji} {action}", "callback_data": f"confirm_{callback_id}_{action}"},
        {"text": "❌ Bỏ qua",         "callback_data": f"skip_{callback_id}"}
    ]]}
    send_text(text, keyboard)

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/alert", methods=["POST"])
def alert():
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

    emoji   = "🔴" if zone_type == "RESISTANCE" else "🟢"
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

    callback_id          = str(int(time.time()))
    pending[callback_id] = data
    send_signal_message(data, callback_id)
    return jsonify({"status": "sent", "callback_id": callback_id}), 200


@app.route("/webhook_tg", methods=["POST"])
def webhook_tg():
    update = request.get_json(force=True, silent=True) or {}

    # ── Xử lý text reply (nhập risk USD) ──────────────────────────
    msg = update.get("message", {})
    if msg:
        chat_id  = str(msg.get("chat", {}).get("id", ""))
        text_in  = msg.get("text", "").strip()
        rs       = risk_state.get(chat_id)

        if rs and rs.get("waiting_risk"):
            try:
                risk_usd = float(text_in)
            except ValueError:
                send_text("⚠️ Vui lòng nhập số tiền hợp lệ (ví dụ: 50)")
                return jsonify({"ok": True}), 200

            callback_id = rs["callback_id"]
            sig         = pending.pop(callback_id, {})
            action      = rs["action"]
            symbol      = sig.get("symbol", "XAUUSD")
            price       = sig.get("price",  "—")
            sl          = sig.get("sl")
            tp          = sig.get("tp")
            tf          = sig.get("tf", "M5")
            emoji       = "🔴" if action == "SELL" else "🟢"

            # Tạo order đưa vào MT5 queue
            order_id = str(uuid.uuid4())[:8]
            mt5_queue[order_id] = {
                "order_id": order_id,
                "action":   action,
                "symbol":   symbol + ".r",  # Exness raw spread
                "price":    price,
                "sl":       sl,
                "tp":       tp,
                "risk_usd": risk_usd,
                "tf":       tf,
                "created":  time.time()
            }

            # Xóa risk_state
            del risk_state[chat_id]

            send_text(
                f"{emoji} <b>ĐÃ XÁC NHẬN {action}</b> — {symbol}\n"
                f"📍 Entry: {price} | {tf}\n"
                f"🛑 SL: {sl}\n"
                f"🎯 TP: {tp}  (RR 1:2)\n"
                f"💰 Risk: ${risk_usd}\n"
                f"⏰ {now_ict()}\n"
                f"⏳ <i>Đang chờ EA MT5 thực thi...</i>"
            )
            return jsonify({"ok": True}), 200

    # ── Xử lý callback query (bấm nút inline) ─────────────────────
    cq = update.get("callback_query")
    if not cq:
        return jsonify({"ok": True}), 200

    cq_id   = cq["id"]
    cq_data = cq.get("data", "")
    message = cq.get("message", {})
    msg_id  = message.get("message_id")
    chat_id = str(message.get("chat", {}).get("id", ""))
    parts   = cq_data.split("_")

    if parts[0] == "confirm" and len(parts) >= 3:
        callback_id = parts[1]
        action      = parts[2]
        sig         = pending.get(callback_id, {})
        emoji       = "🔴" if action == "SELL" else "🟢"

        answer_callback(cq_id, f"✅ Nhập số tiền risk tối đa (USD)")
        edit_message(chat_id, msg_id,
            f"{emoji} <b>XÁC NHẬN {action}</b> — {sig.get('symbol','XAUUSD')}\n"
            f"📍 Entry: {sig.get('price','—')} | SL: {sig.get('sl','—')} | TP: {sig.get('tp','—')}\n\n"
            f"💰 <b>Reply tin nhắn này với số tiền risk tối đa (USD)</b>\n"
            f"<i>Ví dụ: 50</i>"
        )

        # Lưu trạng thái chờ risk
        risk_state[chat_id] = {
            "callback_id":   callback_id,
            "action":        action,
            "waiting_risk":  True
        }

    elif parts[0] == "skip" and len(parts) >= 2:
        cb_id = parts[1]
        sig   = pending.pop(cb_id, {})
        answer_callback(cq_id, "❌ Đã bỏ qua")
        edit_message(chat_id, msg_id,
            f"❌ <b>Đã bỏ qua</b> — {sig.get('symbol','XAUUSD')} {sig.get('tf','')}\n"
            f"⏰ {now_ict()}"
        )
        risk_state.pop(chat_id, None)

    return jsonify({"ok": True}), 200


@app.route("/mt5/pending", methods=["GET"])
def mt5_pending():
    """EA poll endpoint — trả về lệnh đầu tiên trong queue."""
    # Dọn lệnh cũ hơn 5 phút
    now = time.time()
    expired = [k for k, v in mt5_queue.items() if now - v.get("created", 0) > 300]
    for k in expired:
        del mt5_queue[k]

    if not mt5_queue:
        return jsonify({}), 200

    order_id = next(iter(mt5_queue))
    return jsonify(mt5_queue[order_id]), 200


@app.route("/mt5/ack", methods=["POST"])
def mt5_ack():
    """EA báo đã xử lý lệnh → xóa khỏi queue."""
    data     = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id", "")
    status   = data.get("status", "")

    if order_id in mt5_queue:
        sig = mt5_queue.pop(order_id)
        emoji = "✅" if status == "filled" else "❌"
        send_text(
            f"{emoji} <b>MT5: {status.upper()}</b>\n"
            f"📍 {sig.get('action')} {sig.get('symbol')} @ {sig.get('price')}\n"
            f"⏰ {now_ict()}"
        )

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
