import os
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")

ICT = timezone(timedelta(hours=7))


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=10)
    return r.ok


def format_message(data: dict) -> str:
    symbol    = data.get("symbol", "XAUUSD")
    price     = data.get("price", "—")
    zone_type = data.get("zone_type", "ZONE").upper()   # RESISTANCE / SUPPORT
    timeframe = data.get("timeframe", "H1").upper()
    note      = data.get("note", "")

    emoji = "🔴" if zone_type == "RESISTANCE" else "🟢"
    now   = datetime.now(ICT).strftime("%H:%M ICT %d/%m")

    lines = [
        f"{emoji} <b>{symbol}</b> — Chạm vùng <b>{zone_type}</b>",
        "━━━━━━━━━━━━━━━━━",
        f"📍 Giá: <b>{price}</b>",
        f"📊 Khung: {timeframe}",
        f"⏰ {now}",
    ]
    if note:
        lines.append(f"📝 {note}")
    lines.append("")
    lines.append("👀 <i>Theo dõi setup M5 / M15</i>")

    return "\n".join(lines)


@app.route("/alert", methods=["POST"])
def alert():
    """
    Nhận webhook từ TradingView.
    TradingView gửi raw text hoặc JSON — xử lý cả hai.
    """
    # TradingView thường gửi Content-Type: application/x-www-form-urlencoded
    # hoặc plain text. Thử parse JSON trước.
    data = {}
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        pass

    # Nếu không phải JSON thì đọc raw text
    if not data:
        raw = request.data.decode("utf-8").strip()
        # Cho phép format đơn giản: "XAUUSD|2345.50|RESISTANCE|H1"
        parts = [p.strip() for p in raw.split("|")]
        keys  = ["symbol", "price", "zone_type", "timeframe", "note"]
        data  = dict(zip(keys, parts))

    if not BOT_TOKEN or not CHAT_ID:
        return jsonify({"error": "BOT_TOKEN or CHAT_ID not set"}), 500

    msg = format_message(data)
    ok  = send_telegram(msg)

    if ok:
        return jsonify({"status": "sent"}), 200
    else:
        return jsonify({"status": "telegram_error"}), 500


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "tv-telegram-relay"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
