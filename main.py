"""
TV → Telegram → MT5 Relay Server v4.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Changes từ v4.0:
  ✅ Telegram diagnostic log — mỗi signal đến đều notify (kể cả reject/ignore)
  ✅ /ping endpoint nhẹ cho UptimeRobot (tránh cold start nặng)
  ✅ Request timing — log thời gian xử lý mỗi request
  ✅ Startup notify — bot báo khi server restart/cold start
  ✅ Signal retry buffer — lưu signal gần nhất, EA có thể retry
  ✅ /debug/last endpoint — xem signal cuối cùng và lý do xử lý
  ✅ Zone auto-activate luôn ghi log chi tiết
"""

import os, json, time, uuid, hashlib, logging, requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta
from threading import Lock
from functools import wraps

# ─── Config ────────────────────────────────────────────────────────────────────
app = Flask(__name__)

BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
CHAT_ID         = os.environ.get("CHAT_ID", "")
API_KEY         = os.environ.get("API_KEY", "changeme")
SYMBOL_SUFFIX   = os.environ.get("SYMBOL_SUFFIX", "")
MAX_PRICE_DEV   = float(os.environ.get("MAX_PRICE_DEV", "5"))
DEFAULT_RR      = float(os.environ.get("DEFAULT_RR", "2.0"))
ZONE_TTL        = int(os.environ.get("ZONE_TTL", "14400"))
ORDER_TTL       = int(os.environ.get("ORDER_TTL", "300"))
SHEET_URL       = os.environ.get("SHEET_URL", "")
STATE_FILE      = "/tmp/relay_state.json"
ICT             = timezone(timedelta(hours=7))

# ─── Diagnostic config ────────────────────────────────────────────────────────
# Gửi TẤT CẢ event lên Telegram để debug signal miss
DIAG_MODE       = os.environ.get("DIAG_MODE", "true").lower() == "true"

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("relay")

# ─── Thread-safe State ─────────────────────────────────────────────────────────
lock = Lock()

zone_state   = {}
pending      = {}
mt5_queue    = {}
risk_state   = {}
seen_signals = {}

# ── Diagnostic state ──
last_signal_log = {}   # Signal cuối cùng + kết quả xử lý
startup_time    = time.time()
request_count   = {"signal": 0, "alert": 0, "mt5_poll": 0}


def save_state():
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
    global zone_state, pending, mt5_queue, risk_state
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
            zone_state = s.get("zone_state", {})
            pending    = s.get("pending", {})
            mt5_queue  = s.get("mt5_queue", {})
            risk_state = s.get("risk_state", {})
            log.info(f"State restored: {len(mt5_queue)} orders, {len(pending)} pending")
    except FileNotFoundError:
        log.info("No saved state — starting fresh")
    except Exception as e:
        log.warning(f"load_state failed: {e}")

load_state()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def now_ict():
    return datetime.now(ICT).strftime("%H:%M ICT %d/%m")


def now_ict_full():
    return datetime.now(ICT).strftime("%H:%M:%S ICT %d/%m")


def verify_api_key():
    key = request.headers.get("X-API-Key") or request.args.get("key")
    return key == API_KEY


def signal_hash(data):
    raw = f"{data.get('symbol','')}{data.get('action','')}{data.get('price','')}{data.get('tf','')}"
    return hashlib.md5(raw.encode()).hexdigest()


def request_timer(f):
    """Decorator đo thời gian xử lý request."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = f(*args, **kwargs)
        elapsed = (time.time() - start) * 1000  # ms
        if elapsed > 2000:
            log.warning(f"⚠️ SLOW REQUEST: {f.__name__} took {elapsed:.0f}ms")
            if DIAG_MODE:
                send_text(f"⚠️ <b>Slow request</b>: {f.__name__} — {elapsed:.0f}ms")
        return result
    return wrapper


def cleanup():
    now = time.time()
    for sym in list(zone_state):
        if now > zone_state[sym].get("expires", 0):
            del zone_state[sym]
            log.info(f"Zone expired: {sym}")
    for oid in list(mt5_queue):
        if now - mt5_queue[oid].get("created", 0) > ORDER_TTL:
            expired = mt5_queue.pop(oid)
            log.warning(f"Order expired: {oid}")
            send_text(f"⚠️ <b>Order hết hạn</b> — EA không poll trong {ORDER_TTL}s\n"
                      f"📍 {expired.get('action')} {expired.get('symbol')} @ {expired.get('price')}")
    for h in list(seen_signals):
        if now - seen_signals[h] > 60:
            del seen_signals[h]
    for cb_id in list(pending):
        if now - pending[cb_id].get("_created", 0) > 600:
            del pending[cb_id]
            risk_state.pop(cb_id, None)


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


def send_diag(text):
    """Gửi diagnostic message — chỉ khi DIAG_MODE bật."""
    if DIAG_MODE:
        send_text(f"🔍 <b>DIAG</b>\n{text}")


def calc_pnl_usd(symbol, lots, entry, exit_price):
    try:
        entry = float(entry)
        exit_price = float(exit_price)
        lots = float(lots)
        dist = abs(exit_price - entry)
        sym = symbol.upper().replace(SYMBOL_SUFFIX, "")
        if "XAU" in sym:
            return dist * 100 * lots
        elif "XAG" in sym:
            return dist * 5000 * lots
        elif "BTC" in sym or "ETH" in sym:
            return dist * lots
        elif sym in ("NAS100", "US30", "US500"):
            return dist * 10 * lots
        elif "JPY" in sym:
            return (dist / 0.01) * 10 * lots
        else:
            return (dist / 0.0001) * 10 * lots
    except Exception:
        return 0


def sync_sheet(sig, lot_str=""):
    if not SHEET_URL:
        return
    try:
        symbol  = sig.get("symbol", "XAUUSD").replace(SYMBOL_SUFFIX, "")
        action  = sig.get("action", "")
        entry   = sig.get("price", 0)
        sl      = sig.get("sl", 0)
        tp      = sig.get("tp", 0)
        tf      = sig.get("tf", "M5")
        lot = 0.01
        if lot_str:
            parts = lot_str.split()
            if parts:
                try:
                    lot = float(parts[0])
                except ValueError:
                    pass
        sl_pnl = calc_pnl_usd(symbol, lot, entry, sl) if sl else 0
        tp_pnl = calc_pnl_usd(symbol, lot, entry, tp) if tp else 0
        rr = f"1:{tp_pnl/sl_pnl:.2f}" if sl_pnl > 0 else "-"
        direction = "LONG" if action == "BUY" else "SHORT"
        payload = {
            "time": now_ict(), "pair": symbol, "direction": direction,
            "lot": lot, "entry": entry, "sl": sl, "tp": tp,
            "maxLoss": f"{sl_pnl:.2f}", "maxProfit": f"{tp_pnl:.2f}",
            "rr": rr, "emotion": "confident",
            "reason": f"SemiAutoEA {tf} — VSA+HARSI signal"
        }
        requests.post(SHEET_URL, json=payload, timeout=10)
        log.info(f"Sheet logged: {direction} {symbol} {lot} lot")
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
    if rr is None:
        rr = DEFAULT_RR
    try:
        price = float(price)
        if action == "SELL" and zone_top > 0:
            sl = zone_top
            risk = sl - price
            if risk <= 0:
                return None, None
            tp = round(price - risk * rr, 2)
            return round(sl, 2), tp
        elif action == "BUY" and zone_bottom > 0:
            sl = zone_bottom
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
    sig_type = data.get("signal_type", "vsa")

    emoji    = "🔴" if action == "SELL" else "🟢"
    tf_emoji = "⚡" if tf == "M5" else "🔔"
    interval = "5" if tf == "M5" else "15"
    chart    = f"https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD&interval={interval}"
    header   = f"{tf_emoji} <b>SIGNAL {action} — {symbol} {tf}</b>"
    type_tag = "📐 Divergence" if sig_type == "div" else "📊 VSA Engulf"

    sl_line = f"🛑 SL: <b>{sl}</b>\n" if sl else "⚠️ SL: <i>chưa xác định</i>\n"
    tp_line = f"🎯 TP: <b>{tp}</b>  (RR 1:{DEFAULT_RR})\n" if tp else ""

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📍 Giá: <b>{price}</b>\n"
        f"📊 HARSI: {harsi}\n"
        f"🏷 Type: {type_tag}\n"
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


# ─── Startup notification ──────────────────────────────────────────────────────

def notify_startup():
    """Báo Telegram khi server start/restart — detect cold start."""
    send_text(
        f"🚀 <b>Relay Server v4.1 started</b>\n"
        f"⏰ {now_ict_full()}\n"
        f"📊 State: {len(mt5_queue)} orders, {len(pending)} pending, {len(zone_state)} zones\n"
        f"🔍 Diag mode: {'ON' if DIAG_MODE else 'OFF'}"
    )

notify_startup()


# ═════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def health():
    with lock:
        cleanup()
    return jsonify({
        "status":       "ok",
        "uptime_s":     int(time.time() - startup_time),
        "pending":      len(pending),
        "mt5_queue":    len(mt5_queue),
        "active_zones": len(zone_state),
        "requests":     request_count,
        "diag_mode":    DIAG_MODE,
    }), 200


@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    """Lightweight ping cho UptimeRobot — không cleanup, không lock."""
    return "pong", 200


@app.route("/alert", methods=["POST"])
@request_timer
def alert():
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
        request_count["alert"] += 1
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
@request_timer
def signal_route():
    if not verify_api_key():
        log.warning(f"Unauthorized /signal from {request.remote_addr}")
        send_diag(f"❌ Unauthorized /signal từ {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

    data = {}
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        pass
    if not data:
        raw = request.data.decode("utf-8", errors="replace").strip()
        # Loại bỏ BOM và ký tự ẩn đầu string
        raw = raw.lstrip("\ufeff\u200b\u00a0")
        log.info(f"Raw body (first 500): {repr(raw[:500])}")
        send_diag(
            f"📦 Raw body nhận được:\n"
            f"<code>{raw[:400]}</code>\n"
            f"📏 Length: {len(raw)} bytes\n"
            f"🔤 First 10 chars: <code>{repr(raw[:10])}</code>"
        )
        try:
            data = json.loads(raw)
        except Exception as e:
            send_diag(
                f"❌ JSON parse FAILED:\n"
                f"🐛 Error: <code>{str(e)[:200]}</code>\n"
                f"📦 Body: <code>{raw[:400]}</code>"
            )
            return jsonify({"error": "invalid body"}), 400

    symbol = data.get("symbol", "XAUUSD")
    action = data.get("action", "")
    price  = data.get("price", "")
    tf     = data.get("tf", "M5")
    sig_type = data.get("signal_type", "vsa")

    with lock:
        request_count["signal"] += 1
        cleanup()

        # ── Log: signal nhận được ──
        log.info(f"📨 Signal received: {action} {symbol} @ {price} [{sig_type}] {tf}")
        send_diag(
            f"📨 Signal nhận: <b>{action} {symbol}</b> @ {price}\n"
            f"🏷 Type: {sig_type} | TF: {tf}\n"
            f"📦 Raw: <code>{json.dumps(data, ensure_ascii=False)[:300]}</code>"
        )

        # Auto-activate zone
        sig_top = 0
        sig_bot = 0
        try:
            sig_top = float(data.get("top", 0))
            sig_bot = float(data.get("bottom", 0))
        except Exception:
            pass

        if sig_top > 0 and sig_bot > 0:
            zone_state[symbol] = {
                "active":  True,
                "expires": time.time() + ZONE_TTL,
                "type":    data.get("zone_type", "ZONE").upper(),
                "top":     sig_top,
                "bottom":  sig_bot,
            }
            log.info(f"Zone auto-activated: {symbol} [{sig_bot} - {sig_top}]")

        zs = zone_state.get(symbol)

        # Check zone active
        if not zs or not zs.get("active") or time.time() > zs.get("expires", 0):
            reason = "zone_not_active"
            log.info(f"Signal IGNORED — {reason}: {symbol}")
            send_diag(
                f"⏭ Signal <b>IGNORED</b>: {action} {symbol}\n"
                f"❓ Lý do: Zone không active\n"
                f"📋 Zones hiện tại: {list(zone_state.keys())}"
            )
            last_signal_log.update({"time": now_ict_full(), "data": data, "result": reason})
            return jsonify({"status": "ignored", "reason": reason}), 200

        # Duplicate protection
        sh = signal_hash(data)
        if sh in seen_signals:
            reason = "duplicate"
            log.warning(f"Signal DUPLICATE ignored: {symbol} {action}")
            send_diag(f"⏭ Signal <b>DUPLICATE</b>: {action} {symbol} @ {price}")
            last_signal_log.update({"time": now_ict_full(), "data": data, "result": reason})
            return jsonify({"status": "ignored", "reason": reason}), 200
        seen_signals[sh] = time.time()

        # Calc SL/TP
        div_sl = data.get("div_sl")

        if div_sl and sig_type == "div":
            try:
                sl = round(float(div_sl), 2)
                price_f = float(price)
                risk = abs(price_f - sl)
                tp = round(price_f + risk * DEFAULT_RR, 2) if action == "BUY" else round(price_f - risk * DEFAULT_RR, 2)
            except Exception:
                sl, tp = None, None
        else:
            sl, tp = calc_sl_tp(action, price, zs.get("top", 0), zs.get("bottom", 0))

        data["sl"] = sl
        data["tp"] = tp

        # Validate SL
        if sl is None:
            reason = "no_sl"
            log.warning(f"Signal REJECTED — cannot calc SL: {action} {symbol} @ {price}")
            send_diag(
                f"❌ Signal <b>REJECTED</b>: {action} {symbol}\n"
                f"❓ Lý do: Không tính được SL\n"
                f"📐 Zone: {zs.get('bottom')} — {zs.get('top')}\n"
                f"📍 Price: {price} | div_sl: {div_sl}"
            )
            send_text(f"⚠️ <b>Signal rejected</b> — Không thể tính SL\n"
                      f"📍 {action} {symbol} @ {price}\n"
                      f"Zone: {zs.get('bottom')} — {zs.get('top')}")
            last_signal_log.update({"time": now_ict_full(), "data": data, "result": reason})
            return jsonify({"status": "rejected", "reason": reason}), 200

        callback_id          = str(uuid.uuid4())[:12]
        data["_created"]     = time.time()
        pending[callback_id] = data
        save_state()

        last_signal_log.update({
            "time": now_ict_full(), "data": data,
            "result": "sent_to_telegram", "callback_id": callback_id
        })

    log.info(f"Signal → Telegram: {action} {symbol} @ {price} | SL:{sl} TP:{tp}")
    send_signal_message(data, callback_id)
    return jsonify({"status": "sent", "callback_id": callback_id}), 200


@app.route("/webhook_tg", methods=["POST"])
def webhook_tg():
    update = request.get_json(force=True, silent=True) or {}

    # ── Text reply (risk USD) ──
    msg = update.get("message", {})
    if msg:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text_in = msg.get("text", "").strip()

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

    # ── Callback query (inline buttons) ──
    cq = update.get("callback_query")
    if not cq:
        return jsonify({"ok": True}), 200

    cq_id   = cq["id"]
    cq_data = cq.get("data", "")
    message = cq.get("message", {})
    msg_id  = message.get("message_id")
    chat_id = str(message.get("chat", {}).get("id", ""))
    parts   = cq_data.split("_", 2)

    if parts[0] == "confirm" and len(parts) >= 3:
        callback_id = parts[1]
        action      = parts[2]

        with lock:
            sig = pending.get(callback_id, {})
            if not sig:
                answer_callback(cq_id, "⚠️ Signal đã hết hạn")
                return jsonify({"ok": True}), 200

            answer_callback(cq_id, "✅ Nhập số tiền risk tối đa (USD)")
            edit_message(chat_id, msg_id,
                f"{'🔴' if action == 'SELL' else '🟢'} <b>XÁC NHẬN {action}</b> — {sig.get('symbol','XAUUSD')}\n"
                f"📍 Entry: {sig.get('price','—')} | SL: {sig.get('sl','—')} | TP: {sig.get('tp','—')}\n\n"
                f"💰 <b>Reply với số tiền risk tối đa (USD)</b>\n"
                f"<i>Ví dụ: 50</i>"
            )

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
    if not verify_api_key():
        return jsonify({"error": "unauthorized"}), 401

    with lock:
        request_count["mt5_poll"] += 1
        cleanup()
        if not mt5_queue:
            return "", 204
        order_id = next(iter(mt5_queue))
        return jsonify(mt5_queue[order_id]), 200


@app.route("/mt5/ack", methods=["POST"])
def mt5_ack():
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
            if status == "filled":
                sync_sheet(sig, detail)
        else:
            log.warning(f"Ack for unknown order: {order_id}")

    return jsonify({"ok": True}), 200


@app.route("/debug/last", methods=["GET"])
def debug_last():
    """Xem signal cuối cùng và kết quả xử lý — dùng để debug."""
    if not verify_api_key():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(last_signal_log), 200


@app.route("/status", methods=["GET"])
def status():
    if not verify_api_key():
        return jsonify({"error": "unauthorized"}), 401

    with lock:
        cleanup()
        return jsonify({
            "uptime_s":   int(time.time() - startup_time),
            "requests":   request_count,
            "diag_mode":  DIAG_MODE,
            "zones":      {k: {**v, "expires_in": max(0, int(v["expires"] - time.time()))} for k, v in zone_state.items()},
            "pending":    {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in pending.items()},
            "mt5_queue":  mt5_queue,
            "risk_wait":  {k: v for k, v in risk_state.items() if v.get("waiting_risk")},
            "last_signal": last_signal_log,
        }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
