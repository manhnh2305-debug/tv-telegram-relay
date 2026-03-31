"""
TV → Telegram → MT5 Relay Server v4.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fixes từ v3:
  ✅ API key authentication (chặn lệnh giả)
  ✅ UUID callback_id (không collision)
  ✅ Duplicate signal protection (TradingView retry-safe)
  ✅ Symbol suffix configurable (không hardcode .r)
  ✅ risk_state per callback (không ghi đè khi 2 signal liên tiếp)
  ✅ Auto-cleanup expired data
  ✅ Structured logging
  ✅ Max price deviation check
  ✅ Persistent state via JSON file (survive Render restart)
"""

import os, json, time, uuid, hashlib, logging, requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta
from threading import Lock

# ─── Config ────────────────────────────────────────────────────────────────────
app = Flask(__name__)

BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
CHAT_ID         = os.environ.get("CHAT_ID", "")
API_KEY         = os.environ.get("API_KEY", "changeme")       # Auth cho TradingView + EA
SYMBOL_SUFFIX   = os.environ.get("SYMBOL_SUFFIX", "")         # ".r" cho Exness raw, "" nếu ko cần
MAX_PRICE_DEV   = float(os.environ.get("MAX_PRICE_DEV", "5")) # USD — bỏ signal nếu giá chạy quá xa
DEFAULT_RR      = float(os.environ.get("DEFAULT_RR", "2.0"))
ZONE_TTL        = int(os.environ.get("ZONE_TTL", "14400"))     # 4h
ORDER_TTL       = int(os.environ.get("ORDER_TTL", "300"))      # 5 min
SHEET_URL       = os.environ.get("SHEET_URL", "")              # Google Apps Script URL cho trade log
STATE_FILE      = "/tmp/relay_state.json"
ICT             = timezone(timedelta(hours=7))

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("relay")

# ─── Thread-safe State ─────────────────────────────────────────────────────────
lock = Lock()

zone_state  = {}   # { symbol: { active, expires, type, top, bottom } }
pending     = {}   # { callback_id: signal_data }  — chờ xác nhận Telegram
mt5_queue   = {}   # { order_id: order_data }       — chờ EA poll
risk_state  = {}   # { callback_id: { action, waiting_risk } }  — keyed by CALLBACK, not chat_id
seen_signals = {}  # { signal_hash: timestamp }      — duplicate protection


def save_state():
    """Persist critical state to survive Render restart."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "zone_state":  zone_state,
                "pending":     pending,
                "mt5_queue":   mt5_queue,
                "risk_state":  risk_state,
            }, f)
    except Exception as e:
        log.warning(f"save_state failed: {e}")


def load_state():
    """Restore state on startup."""
    global zone_state, pending, mt5_queue, risk_state
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
            zone_state = s.get("zone_state", {})
            pending    = s.get("pending", {})
            mt5_queue  = s.get("mt5_queue", {})
            risk_state = s.get("risk_state", {})
            log.info(f"State restored: {len(mt5_queue)} orders, {len(pending)} pending signals")
    except FileNotFoundError:
        log.info("No saved state — starting fresh")
    except Exception as e:
        log.warning(f"load_state failed: {e}")

load_state()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def now_ict():
    return datetime.now(ICT).strftime("%H:%M ICT %d/%m")


def verify_api_key():
    """Check API key from header or query param."""
    key = request.headers.get("X-API-Key") or request.args.get("key")
    return key == API_KEY


def signal_hash(data):
    """Tạo hash từ signal để detect duplicate (TradingView retry)."""
    raw = f"{data.get('symbol','')}{data.get('action','')}{data.get('price','')}{data.get('tf','')}"
    return hashlib.md5(raw.encode()).hexdigest()


def cleanup():
    """Dọn dẹp data hết hạn."""
    now = time.time()

    # Zone hết hạn
    for sym in list(zone_state):
        if now > zone_state[sym].get("expires", 0):
            del zone_state[sym]
            log.info(f"Zone expired: {sym}")

    # Order hết hạn (5 phút)
    for oid in list(mt5_queue):
        if now - mt5_queue[oid].get("created", 0) > ORDER_TTL:
            expired = mt5_queue.pop(oid)
            log.warning(f"Order expired (EA không poll): {oid} — {expired.get('action')} {expired.get('symbol')}")
            send_text(f"⚠️ <b>Order hết hạn</b> — EA không poll trong {ORDER_TTL}s\n"
                      f"📍 {expired.get('action')} {expired.get('symbol')} @ {expired.get('price')}")

    # Seen signals cũ hơn 60s
    for h in list(seen_signals):
        if now - seen_signals[h] > 60:
            del seen_signals[h]

    # Pending signals cũ hơn 10 phút
    for cb_id in list(pending):
        sig = pending[cb_id]
        if now - sig.get("_created", 0) > 600:
            del pending[cb_id]
            risk_state.pop(cb_id, None)
            log.info(f"Pending signal expired: {cb_id}")


def send_text(text, reply_markup=None):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return None


def calc_pnl_usd(symbol, lots, entry, exit_price):
    """Tính P&L theo USD — dùng Exness CFD contract specs."""
    try:
        entry = float(entry)
        exit_price = float(exit_price)
        lots = float(lots)
        dist = abs(exit_price - entry)
        sym = symbol.upper().replace(SYMBOL_SUFFIX, "")

        # Gold: 1 lot = 100 oz
        if "XAU" in sym:
            return dist * 100 * lots
        # Silver: 1 lot = 5000 oz
        elif "XAG" in sym:
            return dist * 5000 * lots
        # BTC/ETH: 1 lot = 1 unit
        elif "BTC" in sym or "ETH" in sym:
            return dist * lots
        # Indices: contract = 10
        elif sym in ("NAS100", "US30", "US500"):
            return dist * 10 * lots
        # JPY pairs: pip = 0.01
        elif "JPY" in sym:
            return (dist / 0.01) * 10 * lots
        # Standard forex: pip = 0.0001
        else:
            return (dist / 0.0001) * 10 * lots
    except Exception:
        return 0


def sync_sheet(sig, lot_str=""):
    """Gửi trade data lên Google Sheets qua Apps Script."""
    if not SHEET_URL:
        log.info("SHEET_URL not configured — skip trade log")
        return

    try:
        symbol  = sig.get("symbol", "XAUUSD").replace(SYMBOL_SUFFIX, "")
        action  = sig.get("action", "")
        entry   = sig.get("price", 0)
        sl      = sig.get("sl", 0)
        tp      = sig.get("tp", 0)
        risk    = sig.get("risk_usd", 0)
        tf      = sig.get("tf", "M5")

        # Parse lot từ detail string ("0.15 lot @ spread $0.25")
        lot = 0.01
        if lot_str:
            parts = lot_str.split()
            if parts:
                try:
                    lot = float(parts[0])
                except ValueError:
                    pass

        # Tính P&L
        sl_pnl = calc_pnl_usd(symbol, lot, entry, sl) if sl else 0
        tp_pnl = calc_pnl_usd(symbol, lot, entry, tp) if tp else 0
        rr = f"1:{tp_pnl/sl_pnl:.2f}" if sl_pnl > 0 else "-"

        direction = "LONG" if action == "BUY" else "SHORT"

        payload = {
            "time":      now_ict(),
            "pair":      symbol,
            "direction": direction,
            "lot":       lot,
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "maxLoss":   f"{sl_pnl:.2f}",
            "maxProfit": f"{tp_pnl:.2f}",
            "rr":        rr,
            "emotion":   "confident",
            "reason":    f"SemiAutoEA {tf} — VSA+HARSI signal"
        }

        requests.post(SHEET_URL, json=payload, timeout=10)
        log.info(f"Trade logged to Sheet: {direction} {symbol} {lot} lot | SL ${sl_pnl:.2f} TP ${tp_pnl:.2f}")

    except Exception as e:
        log.error(f"Sheet sync failed: {e}")


def edit_message(chat_id, msg_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    try:
        requests.post(url, json={
            "chat_id": chat_id, "message_id": msg_id,
            "text": text, "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        log.error(f"Telegram edit failed: {e}")


def answer_callback(cq_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": cq_id, "text": text}, timeout=5)
    except Exception as e:
        log.error(f"Telegram callback answer failed: {e}")


def calc_sl_tp(action, price, zone_top, zone_bottom, rr=None):
    """Tính SL/TP dựa trên zone box, return (sl, tp) hoặc (None, None)."""
    if rr is None:
        rr = DEFAULT_RR
    try:
        price = float(price)
        if action == "SELL" and zone_top > 0:
            sl   = zone_top
            risk = sl - price
            if risk <= 0:
                return None, None
            tp = round(price - risk * rr, 2)
            return round(sl, 2), tp
        elif action == "BUY" and zone_bottom > 0:
            sl   = zone_bottom
            risk = price - sl
            if risk <= 0:
                return None, None
            tp = round(price + risk * rr, 2)
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
    sl     = data.get("sl")
    tp     = data.get("tp")

    emoji    = "🔴" if action == "SELL" else "🟢"
    tf_emoji = "⚡" if tf == "M5" else "🔔"
    interval = "5" if tf == "M5" else "15"
    chart    = f"https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD&interval={interval}"
    header   = f"{tf_emoji} <b>SIGNAL {action} — {symbol} {tf}</b>"

    sl_line = f"🛑 SL: <b>{sl}</b>\n" if sl else "⚠️ SL: <i>chưa xác định</i>\n"
    tp_line = f"🎯 TP: <b>{tp}</b>  (RR 1:{DEFAULT_RR})\n" if tp else ""

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


# ─── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    with lock:
        cleanup()
    return jsonify({
        "status":        "ok",
        "pending":       len(pending),
        "mt5_queue":     len(mt5_queue),
        "active_zones":  len(zone_state),
    }), 200


@app.route("/alert", methods=["POST"])
def alert():
    """TradingView zone alert → Telegram notification."""
    if not verify_api_key():
        log.warning(f"Unauthorized /alert from {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

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

    with lock:
        zone_state[symbol] = {
            "active":  True,
            "expires": time.time() + ZONE_TTL,
            "type":    zone_type,
            "top":     zone_top,
            "bottom":  zone_bottom,
        }
        save_state()

    log.info(f"Zone active: {symbol} {zone_type} [{zone_bottom} - {zone_top}] @ {price}")

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
def signal_route():
    """TradingView signal → Telegram confirmation button."""
    if not verify_api_key():
        log.warning(f"Unauthorized /signal from {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

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

    with lock:
        cleanup()
        zs = zone_state.get(symbol)

        # Check zone active
        if not zs or not zs.get("active") or time.time() > zs.get("expires", 0):
            log.info(f"Signal ignored — zone not active: {symbol}")
            return jsonify({"status": "ignored", "reason": "zone_not_active"}), 200

        # Duplicate protection
        sh = signal_hash(data)
        if sh in seen_signals:
            log.warning(f"Duplicate signal ignored: {symbol} {data.get('action')}")
            return jsonify({"status": "ignored", "reason": "duplicate"}), 200
        seen_signals[sh] = time.time()

        # Calc SL/TP from zone
        action = data.get("action", "")
        price  = data.get("price", 0)
        sl, tp = calc_sl_tp(action, price, zs.get("top", 0), zs.get("bottom", 0))
        data["sl"] = sl
        data["tp"] = tp

        # Validate SL exists
        if sl is None:
            log.warning(f"Signal rejected — cannot calc SL: {action} {symbol} @ {price}")
            send_text(f"⚠️ <b>Signal rejected</b> — Không thể tính SL\n"
                      f"📍 {action} {symbol} @ {price}\n"
                      f"Zone: {zs.get('bottom')} — {zs.get('top')}")
            return jsonify({"status": "rejected", "reason": "no_sl"}), 200

        callback_id          = str(uuid.uuid4())[:12]
        data["_created"]     = time.time()
        pending[callback_id] = data
        save_state()

    log.info(f"Signal sent to Telegram: {data.get('action')} {symbol} @ {price} | SL:{sl} TP:{tp}")
    send_signal_message(data, callback_id)
    return jsonify({"status": "sent", "callback_id": callback_id}), 200


@app.route("/webhook_tg", methods=["POST"])
def webhook_tg():
    """Telegram webhook — xử lý button click + risk USD input."""
    update = request.get_json(force=True, silent=True) or {}

    # ── Xử lý text reply (nhập risk USD) ──────────────────────────
    msg = update.get("message", {})
    if msg:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text_in = msg.get("text", "").strip()

        # Tìm risk_state nào đang waiting_risk cho chat này
        with lock:
            active_cb = None
            for cb_id, rs in risk_state.items():
                if rs.get("waiting_risk") and rs.get("chat_id") == chat_id:
                    active_cb = cb_id
                    break

            if active_cb:
                rs = risk_state[active_cb]
                try:
                    risk_usd = float(text_in)
                    if risk_usd <= 0 or risk_usd > 10000:
                        raise ValueError("out of range")
                except ValueError:
                    send_text("⚠️ Vui lòng nhập số tiền hợp lệ (1 - 10000 USD)")
                    return jsonify({"ok": True}), 200

                sig      = pending.pop(active_cb, {})
                action   = rs["action"]
                symbol   = sig.get("symbol", "XAUUSD")
                price    = sig.get("price",  "—")
                sl       = sig.get("sl")
                tp       = sig.get("tp")
                tf       = sig.get("tf", "M5")
                emoji    = "🔴" if action == "SELL" else "🟢"

                # Tạo order → MT5 queue
                order_id = str(uuid.uuid4())[:8]
                mt5_symbol = symbol + SYMBOL_SUFFIX if SYMBOL_SUFFIX else symbol

                mt5_queue[order_id] = {
                    "order_id": order_id,
                    "action":   action,
                    "symbol":   mt5_symbol,
                    "price":    price,
                    "sl":       sl,
                    "tp":       tp,
                    "risk_usd": risk_usd,
                    "tf":       tf,
                    "max_dev":  MAX_PRICE_DEV,
                    "created":  time.time()
                }

                del risk_state[active_cb]
                save_state()

                log.info(f"Order queued: {order_id} — {action} {mt5_symbol} risk=${risk_usd}")

                send_text(
                    f"{emoji} <b>ĐÃ XÁC NHẬN {action}</b> — {symbol}\n"
                    f"📍 Entry: {price} | {tf}\n"
                    f"🛑 SL: {sl}\n"
                    f"🎯 TP: {tp}  (RR 1:{DEFAULT_RR})\n"
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
    parts   = cq_data.split("_", 2)  # ["confirm", "callbackid", "ACTION"] or ["skip", "callbackid"]

    if parts[0] == "confirm" and len(parts) >= 3:
        callback_id = parts[1]
        action      = parts[2]

        with lock:
            sig = pending.get(callback_id, {})
            if not sig:
                answer_callback(cq_id, "⚠️ Signal đã hết hạn")
                return jsonify({"ok": True}), 200

            emoji = "🔴" if action == "SELL" else "🟢"

            answer_callback(cq_id, "✅ Nhập số tiền risk tối đa (USD)")
            edit_message(chat_id, msg_id,
                f"{emoji} <b>XÁC NHẬN {action}</b> — {sig.get('symbol','XAUUSD')}\n"
                f"📍 Entry: {sig.get('price','—')} | SL: {sig.get('sl','—')} | TP: {sig.get('tp','—')}\n\n"
                f"💰 <b>Reply với số tiền risk tối đa (USD)</b>\n"
                f"<i>Ví dụ: 50</i>"
            )

            # Lưu risk_state keyed by callback_id (không phải chat_id → tránh ghi đè)
            risk_state[callback_id] = {
                "callback_id":  callback_id,
                "chat_id":      chat_id,
                "action":       action,
                "waiting_risk": True
            }
            save_state()

    elif parts[0] == "skip" and len(parts) >= 2:
        cb_id = parts[1]
        with lock:
            sig = pending.pop(cb_id, {})
            risk_state.pop(cb_id, None)
            save_state()

        answer_callback(cq_id, "❌ Đã bỏ qua")
        edit_message(chat_id, msg_id,
            f"❌ <b>Đã bỏ qua</b> — {sig.get('symbol','XAUUSD')} {sig.get('tf','')}\n"
            f"⏰ {now_ict()}"
        )

    return jsonify({"ok": True}), 200


@app.route("/mt5/pending", methods=["GET"])
def mt5_pending():
    """EA poll endpoint — trả về lệnh đầu tiên trong queue."""
    if not verify_api_key():
        return jsonify({"error": "unauthorized"}), 401

    with lock:
        cleanup()

        if not mt5_queue:
            return "", 204  # No Content — EA phân biệt được vs lỗi

        order_id = next(iter(mt5_queue))
        return jsonify(mt5_queue[order_id]), 200


@app.route("/mt5/ack", methods=["POST"])
def mt5_ack():
    """EA báo đã xử lý lệnh → xóa khỏi queue, notify Telegram."""
    if not verify_api_key():
        return jsonify({"error": "unauthorized"}), 401

    data     = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id", "")
    status   = data.get("status", "")
    detail   = data.get("detail", "")

    with lock:
        if order_id in mt5_queue:
            sig = mt5_queue.pop(order_id)
            save_state()

            emoji = "✅" if status == "filled" else "❌"
            detail_line = f"\n📝 {detail}" if detail else ""
            send_text(
                f"{emoji} <b>MT5: {status.upper()}</b>\n"
                f"📍 {sig.get('action')} {sig.get('symbol')} @ {sig.get('price')}\n"
                f"💰 Risk: ${sig.get('risk_usd', '—')}"
                f"{detail_line}\n"
                f"⏰ {now_ict()}"
            )
            log.info(f"Order ack: {order_id} — {status} {detail}")

            # Auto-log vào Google Sheets khi lệnh filled
            if status == "filled":
                sync_sheet(sig, detail)
        else:
            log.warning(f"Ack for unknown order: {order_id}")

    return jsonify({"ok": True}), 200


@app.route("/status", methods=["GET"])
def status():
    """Debug endpoint — xem trạng thái hiện tại."""
    if not verify_api_key():
        return jsonify({"error": "unauthorized"}), 401

    with lock:
        cleanup()
        return jsonify({
            "zones":     {k: {**v, "expires_in": max(0, int(v["expires"] - time.time()))} for k, v in zone_state.items()},
            "pending":   {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in pending.items()},
            "mt5_queue": mt5_queue,
            "risk_wait": {k: v for k, v in risk_state.items() if v.get("waiting_risk")},
        }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
