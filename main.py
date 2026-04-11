"""
TV → Telegram → MT5 Relay Server v4.3.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Changes từ v4.3.0:
  ✅ v4.3.1: Crypto → Signal Bot + EA (giữ trade Exness)
             + ĐỒNG THỜI post @ddd_crypto_signal NGAY (không đợi EA ack)
             Forex/XAU → Signal Bot + EA → ack → rồi mới post channel (giữ nguyên)
             Bypass zone check cho crypto (Pine không set zones)
  ─── v4.3.0 features giữ nguyên ───
  ✅ v4.3.0: Channel Router — route signal đến 3 kênh riêng biệt theo symbol
  ✅ v4.3.0: Giữ legacy post vào @goldsignalnodelete (tắt bằng LEGACY_PUBLIC=false)
  ─── v4.2.x features giữ nguyên ───
  ✅ v4.2.3: FIX round SL/TP theo digits per symbol
  ✅ v4.2.2: FIX sync_sheet
  ✅ v4.2.1: Bypass zone khi zone_type = "NONE"
  ✅ v4.2: Auto post signal sang kênh PUBLIC khi MT5 FILLED
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
PUBLIC_CHAT_ID  = os.environ.get("PUBLIC_CHAT_ID", "@goldsignalnodelete")
API_KEY         = os.environ.get("API_KEY", "changeme")
SYMBOL_SUFFIX   = os.environ.get("SYMBOL_SUFFIX", "")
MAX_PRICE_DEV   = float(os.environ.get("MAX_PRICE_DEV", "10"))
DEFAULT_RR      = float(os.environ.get("DEFAULT_RR", "2.0"))
ZONE_TTL        = int(os.environ.get("ZONE_TTL", "14400"))
ORDER_TTL       = int(os.environ.get("ORDER_TTL", "300"))
SHEET_URL       = os.environ.get("SHEET_URL", "")
STATE_FILE      = "/tmp/relay_state.json"
ICT             = timezone(timedelta(hours=7))

DIAG_MODE       = os.environ.get("DIAG_MODE", "true").lower() == "true"

# ─── v4.3.0: Channel Router Config ────────────────────────────────────────────
XAU_CHANNEL_ID    = os.environ.get("XAU_CHANNEL_ID", "")
FOREX_CHANNEL_ID  = os.environ.get("FOREX_CHANNEL_ID", "")
CRYPTO_CHANNEL_ID = os.environ.get("CRYPTO_CHANNEL_ID", "")

LEGACY_PUBLIC = os.environ.get("LEGACY_PUBLIC", "true").lower() == "true"

# Symbol sets cho routing
_XAU_SYMBOLS = {"XAUUSD"}
_CRYPTO_SYMBOLS = {
    "BTCUSD", "BTCUSDT", "ETHUSD", "ETHUSDT",
    "SOLUSD", "SOLUSDT", "BNBUSD", "BNBUSDT",
    "XRPUSD", "XRPUSDT", "ADAUSD", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
}

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

last_signal_log = {}
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

def now_ict_date():
    return datetime.now(ICT).strftime("%Y-%m-%d")

def now_ict_time():
    return datetime.now(ICT).strftime("%H:%M")

def verify_api_key():
    key = request.headers.get("X-API-Key") or request.args.get("key")
    return key == API_KEY

def signal_hash(data):
    raw = f"{data.get('symbol','')}{data.get('action','')}{data.get('price','')}{data.get('tf','')}"
    return hashlib.md5(raw.encode()).hexdigest()

def get_digits(symbol):
    s = symbol.upper().replace(SYMBOL_SUFFIX, "")
    if "XAU" in s:                                      return 2
    if "XAG" in s:                                      return 3
    if "BTC" in s:                                      return 1
    if "ETH" in s:                                      return 2
    if "OIL" in s or "WTI" in s or "BRENT" in s:       return 3
    if "NAS" in s or "US30" in s or "US500" in s:       return 2
    if "JPY" in s:                                      return 3
    return 5

def request_timer(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = f(*args, **kwargs)
        elapsed = (time.time() - start) * 1000
        if elapsed > 2000:
            log.warning(f"SLOW REQUEST: {f.__name__} took {elapsed:.0f}ms")
            if DIAG_MODE:
                send_text(f"<b>Slow request</b>: {f.__name__} — {elapsed:.0f}ms")
        return result
    return wrapper

def cleanup():
    now = time.time()
    for sym in list(zone_state):
        if now > zone_state[sym].get("expires", 0):
            del zone_state[sym]
    for oid in list(mt5_queue):
        if now - mt5_queue[oid].get("created", 0) > ORDER_TTL:
            expired = mt5_queue.pop(oid)
            send_text(f"<b>Order het han</b> — EA khong poll trong {ORDER_TTL}s\n"
                      f"{expired.get('action')} {expired.get('symbol')} @ {expired.get('price')}")
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
        return requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return None

def send_diag(text):
    if DIAG_MODE:
        send_text(f"<b>DIAG</b>\n{text}")


# ═════════════════════════════════════════════════════════════════════════════
# CHANNEL ROUTER (v4.3.0, giữ nguyên)
# ═════════════════════════════════════════════════════════════════════════════

def classify_symbol(symbol):
    sym = symbol.upper().replace(SYMBOL_SUFFIX, "").strip()
    if sym in _XAU_SYMBOLS:    return "xau"
    if sym in _CRYPTO_SYMBOLS: return "crypto"
    return "forex"


def to_mt5_symbol(symbol):
    sym = symbol.upper().replace(SYMBOL_SUFFIX, "").strip()
    if classify_symbol(sym) == "crypto" and sym.endswith("USDT"):
        sym = sym[:-1]
    if SYMBOL_SUFFIX:
        return sym + SYMBOL_SUFFIX
    return sym

def get_channel_id(channel_type):
    return {"xau": XAU_CHANNEL_ID, "forex": FOREX_CHANNEL_ID, "crypto": CRYPTO_CHANNEL_ID}.get(channel_type, "")

def format_channel_message(sig, channel_type):
    action   = sig.get("action", "")
    symbol   = sig.get("symbol", "XAUUSD").replace(SYMBOL_SUFFIX, "")
    price    = sig.get("price", "—")
    sl       = sig.get("sl", "—")
    tp       = sig.get("tp", "—")
    tf       = sig.get("tf", "M5")
    sig_type = sig.get("signal_type", "vsa")
    trend    = sig.get("trend", "")
    mode     = sig.get("mode", "")
    digits   = get_digits(symbol)

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
        f"{channel_icon} {arrow} <b>{emoji} {direction} {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{tf_tag} | {tf}\n\n"
        f"📍 Entry: <b>{price}</b>\n"
        f"🛡 SL: <b>{sl}</b>\n"
        f"🎯 TP1: <b>{tp1}</b>  — RR 1:1\n"
        f"🏆 TP2: <b>{tp2}</b>  — RR 1:2\n"
        f"💎 TP3: <b>{tp3}</b>  — RR 1:3\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>{reason}</i>\n"
        f"🕐 {now_ict()}\n\n"
        f"#signal #{symbol} #{direction.lower()}\n"
        f"🔔 <b>Đừng Đu Đỉnh</b>"
    )

def send_to_channel(channel_id, text):
    if not channel_id: return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": channel_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
        if r.ok and r.json().get("ok"): return True
        log.error(f"Channel post failed [{channel_id}]: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"Channel post exception [{channel_id}]: {e}")
    return False

def route_to_channels(sig, fill_detail=""):
    symbol       = sig.get("symbol", "XAUUSD").replace(SYMBOL_SUFFIX, "")
    channel_type = classify_symbol(symbol)
    channel_id   = get_channel_id(channel_type)

    routed_ok = False
    if channel_id:
        message   = format_channel_message(sig, channel_type)
        routed_ok = send_to_channel(channel_id, message)
        if routed_ok:
            log.info(f"📡 Routed: {sig.get('action')} {symbol} → {channel_type} ({channel_id})")
            send_text(f"📡 Signal <b>{sig.get('action')} {symbol}</b> → kênh <b>{channel_type.upper()}</b> ✅")
        else:
            log.error(f"📡 Route FAILED: {symbol} → {channel_type} ({channel_id})")
            send_text(f"⚠️ Route <b>FAILED</b>: {sig.get('action')} {symbol} → {channel_type}")

    if LEGACY_PUBLIC and PUBLIC_CHAT_ID:
        send_public_signal_legacy(sig, fill_detail)

    return {"channel_type": channel_type, "channel_id": channel_id, "success": routed_ok}


# ─── Legacy Public Post ───────────────────────────────────────────────────────

def send_public_signal_legacy(sig, fill_detail=""):
    if not PUBLIC_CHAT_ID: return
    action  = sig.get("action", "")
    symbol  = sig.get("symbol", "XAUUSD").replace(SYMBOL_SUFFIX, "")
    price   = sig.get("price", "—")
    sl      = sig.get("sl", "—")
    tp      = sig.get("tp", "—")
    tf      = sig.get("tf", "M5")
    sig_type = sig.get("signal_type", "vsa")
    trend    = sig.get("trend", "")
    mode     = sig.get("mode", "")
    digits   = get_digits(symbol)

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

    text = (
        f"{arrow} <b>{emoji} {direction} {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Entry: <b>{price}</b>\n"
        f"🛡 SL: <b>{sl}</b>\n"
        f"🎯 TP1: <b>{tp1}</b>  — RR 1:1\n"
        f"🏆 TP2: <b>{tp2}</b>  — RR 1:2\n"
        f"💎 TP3: <b>{tp3}</b>  — RR 1:3\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>{reason}</i>\n"
        f"🕐 {now_ict()}  •  {tf}\n\n"
        f"#signal #{symbol} #{direction.lower()}"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": PUBLIC_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10
        )
        if r.ok: log.info(f"Legacy posted: {direction} {symbol} @ {price}")
        else:    log.error(f"Legacy failed: {r.status_code}")
    except Exception as e:
        log.error(f"Legacy exception: {e}")


# ─── sync_sheet (giữ nguyên v4.2.3) ───────────────────────────────────────────

def sync_sheet(sig, lot_str="", order_id=""):
    if not SHEET_URL: return
    try:
        symbol   = sig.get("symbol", "XAUUSD").replace(SYMBOL_SUFFIX, "")
        action   = sig.get("action", "")
        entry    = sig.get("price", 0)
        sl       = sig.get("sl", 0)
        tp       = sig.get("tp", 0)
        tf       = sig.get("tf", "M5")
        sig_type = sig.get("signal_type", "vsa")

        lot, spread = 0.01, 0
        if lot_str:
            parts = lot_str.split()
            if parts:
                try: lot = float(parts[0])
                except ValueError: pass
            if "spread" in lot_str:
                try:
                    spread = float(lot_str[lot_str.index("$") + 1:])
                except (ValueError, IndexError): pass

        rr = ""
        try:
            e, s, t = float(entry), float(sl), float(tp)
            risk, reward = abs(e - s), abs(t - e)
            if risk > 0: rr = f"1:{reward / risk:.2f}"
        except (ValueError, TypeError, ZeroDivisionError): pass

        r = requests.post(SHEET_URL, json={
            "type": "trade", "date": now_ict_date(), "time": now_ict_time(),
            "symbol": symbol, "dir": action, "lot": lot, "open": entry,
            "sl": sl, "tp": tp, "rr": rr, "spread": spread,
            "order_id": order_id, "source": "Relay",
            "reason": f"SemiAutoEA {tf} — {'Divergence' if sig_type == 'div' else 'VSA+HARSI'} signal",
        }, timeout=10)
        if r.status_code in (200, 302):
            log.info(f"Sheet logged: {action} {symbol} {lot} lot @ {entry}")
        else:
            log.error(f"Sheet failed: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Sheet error: {e}")


# ─── Misc helpers (giữ nguyên) ─────────────────────────────────────────────────

def calc_pnl_usd(symbol, lots, entry, exit_price):
    try:
        entry, exit_price, lots = float(entry), float(exit_price), float(lots)
        dist = abs(exit_price - entry)
        sym = symbol.upper().replace(SYMBOL_SUFFIX, "")
        if "XAU" in sym:   return dist * 100 * lots
        elif "XAG" in sym: return dist * 5000 * lots
        elif "BTC" in sym or "ETH" in sym: return dist * lots
        elif sym in ("NAS100", "US30", "US500"): return dist * 10 * lots
        elif "JPY" in sym: return (dist / 0.01) * 10 * lots
        else:              return (dist / 0.0001) * 10 * lots
    except Exception: return 0

def edit_message(chat_id, msg_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        log.error(f"Edit failed: {e}")

def answer_callback(cq_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cq_id, "text": text}, timeout=5)
    except Exception as e:
        log.error(f"Callback failed: {e}")

def calc_sl_tp(action, price, zone_top, zone_bottom, symbol="XAUUSD", rr=None):
    if rr is None: rr = DEFAULT_RR
    digits = get_digits(symbol)
    try:
        price = float(price)
        if action == "SELL" and zone_top > 0:
            sl = zone_top; risk = sl - price
            if risk <= 0: return None, None
            return round(sl, digits), round(price - risk * rr, digits)
        elif action == "BUY" and zone_bottom > 0:
            sl = zone_bottom; risk = price - sl
            if risk <= 0: return None, None
            return round(sl, digits), round(price + risk * rr, digits)
    except Exception: pass
    return None, None

def send_signal_message(data, callback_id):
    symbol   = data.get("symbol", "XAUUSD")
    price    = data.get("price",  "—")
    action   = data.get("action", "—")
    tf       = data.get("tf",     "M5")
    harsi    = data.get("harsi",  "—")
    sl       = data.get("sl")
    tp       = data.get("tp")
    sig_type = data.get("signal_type", "vsa")

    emoji    = "🔴" if action == "SELL" else "🟢"
    tf_emoji = "⚡" if tf == "M5" else "🔔"
    interval = "5" if tf == "M5" else "15"
    chart    = f"https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD&interval={interval}"
    type_tag = "📐 Divergence" if sig_type == "div" else "📊 VSA Engulf"

    sl_line = f"🛑 SL: <b>{sl}</b>\n" if sl else "⚠️ SL: <i>chua xac dinh</i>\n"
    tp_line = f"🎯 TP: <b>{tp}</b>  (RR 1:{DEFAULT_RR})\n" if tp else ""

    text = (
        f"{tf_emoji} <b>SIGNAL {action} — {symbol} {tf}</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📍 Gia: <b>{price}</b>\n"
        f"📊 HARSI: {harsi}\n"
        f"🏷 Type: {type_tag}\n"
        f"{sl_line}{tp_line}"
        f"⏰ {now_ict()}\n"
        f"🔗 <a href='{chart}'>Mo chart {tf}</a>\n\n"
        f"👉 Xac nhan vao lenh:"
    )
    keyboard = {"inline_keyboard": [[
        {"text": f"{emoji} {action}", "callback_data": f"confirm_{callback_id}_{action}"},
        {"text": "❌ Bo qua",         "callback_data": f"skip_{callback_id}"}
    ]]}
    send_text(text, keyboard)


# ─── Startup ───────────────────────────────────────────────────────────────────

def notify_startup():
    ch = []
    if XAU_CHANNEL_ID:    ch.append(f"🥇 XAU → {XAU_CHANNEL_ID}")
    if FOREX_CHANNEL_ID:  ch.append(f"💱 Forex → {FOREX_CHANNEL_ID}")
    if CRYPTO_CHANNEL_ID: ch.append(f"🪙 Crypto → {CRYPTO_CHANNEL_ID}")

    send_text(
        f"🚀 <b>Relay Server v4.3.1 started</b>\n"
        f"⏰ {now_ict_full()}\n"
        f"📊 State: {len(mt5_queue)} orders, {len(pending)} pending, {len(zone_state)} zones\n"
        f"🔍 Diag: {'ON' if DIAG_MODE else 'OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Channel Routing:</b>\n"
        f"{chr(10).join(ch) if ch else '⚠️ No channels'}\n"
        f"{'📢 Legacy: ' + PUBLIC_CHAT_ID if LEGACY_PUBLIC else '📢 Legacy: OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Crypto: Signal Bot + EA + channel NGAY\n"
        f"💱 Forex/XAU: Signal Bot + EA → ack → channel"
    )

notify_startup()


# ═════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def health():
    with lock: cleanup()
    return jsonify({
        "status": "ok", "version": "4.3.1",
        "uptime_s": int(time.time() - startup_time),
        "pending": len(pending), "mt5_queue": len(mt5_queue),
        "active_zones": len(zone_state), "requests": request_count,
        "diag_mode": DIAG_MODE,
        "channel_routing": {
            "xau": XAU_CHANNEL_ID or "(not set)",
            "forex": FOREX_CHANNEL_ID or "(not set)",
            "crypto": (CRYPTO_CHANNEL_ID + " (immediate)") if CRYPTO_CHANNEL_ID else "(not set)",
            "legacy": PUBLIC_CHAT_ID if LEGACY_PUBLIC else "(disabled)",
        },
    }), 200

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    return "pong", 200

@app.route("/alert", methods=["POST"])
@request_timer
def alert():
    if not verify_api_key(): return jsonify({"error": "unauthorized"}), 401

    data = {}
    try: data = request.get_json(force=True, silent=True) or {}
    except Exception: pass
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
            "active": True, "expires": time.time() + ZONE_TTL,
            "type": zone_type, "top": zone_top, "bottom": zone_bottom,
        }
        save_state()

    emoji   = "🔴" if zone_type == "RESISTANCE" else "🟢"
    sl_info = f"\n📐 Box: {zone_bottom} — {zone_top}" if zone_top and zone_bottom else ""
    send_text(
        f"{emoji} <b>{symbol}</b> — Gia vao vung <b>{zone_type}</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📍 Gia: <b>{price}</b>{sl_info}\n"
        f"⏰ {now_ict()}\n"
        f"{'📝 ' + note + chr(10) if note else ''}"
        f"\n👀 <i>Dang theo doi setup M5 / M15...</i>"
    )
    return jsonify({"status": "zone_active", "symbol": symbol}), 200


# ═════════════════════════════════════════════════════════════════════════════
# /signal — MAIN HANDLER
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/signal", methods=["POST"])
@request_timer
def signal_route():
    if not verify_api_key():
        return jsonify({"error": "unauthorized"}), 401

    raw = request.data.decode("utf-8", errors="replace").strip().lstrip("\ufeff\u200b\u00a0")
    log.info(f"/signal raw ({len(raw)} bytes): {repr(raw[:300])}")
    send_diag(f"Raw body ({len(raw)} bytes):\n<code>{raw[:400]}</code>")

    data = {}
    if raw:
        try: data = json.loads(raw)
        except Exception as e:
            log.warning(f"JSON parse failed: {e}")
            return jsonify({"error": "invalid body"}), 400
    if not data:
        return jsonify({"error": "empty body"}), 400

    symbol    = data.get("symbol", "XAUUSD")
    action    = data.get("action", "")
    price     = data.get("price", "")
    tf        = data.get("tf", "M5")
    sig_type  = data.get("signal_type", "vsa")
    zone_type = data.get("zone_type", "").upper()
    digits    = get_digits(symbol)

    is_crypto = classify_symbol(symbol) == "crypto"

    with lock:
        request_count["signal"] += 1
        cleanup()

        log.info(f"Signal: {action} {symbol} @ {price} [{sig_type}] {tf}"
                 f"{' 🪙CRYPTO' if is_crypto else ''}")

        sig_top = sig_bot = 0
        try:
            sig_top = float(data.get("top", 0))
            sig_bot = float(data.get("bottom", 0))
        except Exception: pass

        bypass_zone = (zone_type == "NONE") or is_crypto

        if not bypass_zone:
            if sig_top > 0 and sig_bot > 0:
                zone_state[symbol] = {
                    "active": True, "expires": time.time() + ZONE_TTL,
                    "type": zone_type, "top": sig_top, "bottom": sig_bot,
                }

            zs = zone_state.get(symbol)
            if not zs or not zs.get("active") or time.time() > zs.get("expires", 0):
                last_signal_log.update({"time": now_ict_full(), "data": data, "result": "zone_not_active"})
                return jsonify({"status": "ignored", "reason": "zone_not_active"}), 200
        else:
            zs = None

        sh = signal_hash(data)
        if sh in seen_signals:
            last_signal_log.update({"time": now_ict_full(), "data": data, "result": "duplicate"})
            return jsonify({"status": "ignored", "reason": "duplicate"}), 200
        seen_signals[sh] = time.time()

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

        if sl is None:
            send_text(f"<b>Signal rejected</b> — Khong the tinh SL\n{action} {symbol} @ {price}")
            last_signal_log.update({"time": now_ict_full(), "data": data, "result": "no_sl"})
            return jsonify({"status": "rejected", "reason": "no_sl"}), 200

        callback_id      = str(uuid.uuid4())[:12]
        data["_created"] = time.time()
        pending[callback_id] = data
        save_state()

        last_signal_log.update({
            "time": now_ict_full(), "data": data,
            "result": "sent_to_telegram", "callback_id": callback_id
        })

        crypto_copy = dict(data) if is_crypto else None

    log.info(f"→ Signal Bot: {action} {symbol} @ {price} | SL:{sl} TP:{tp}")
    send_signal_message(data, callback_id)

    if crypto_copy:
        log.info(f"🪙 → Crypto channel: {action} {symbol} @ {price}")
        try:
            result = route_to_channels(crypto_copy)
            log.info(f"📡 Crypto route: {result}")
        except Exception as e:
            log.error(f"📡 Crypto route error: {e}")
            send_text(f"⚠️ Crypto route error: {e}")

    return jsonify({"status": "sent", "callback_id": callback_id}), 200


# ═════════════════════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/webhook_tg", methods=["POST"])
def webhook_tg():
    update = request.get_json(force=True, silent=True) or {}

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
                    if risk_usd <= 0 or risk_usd > 10000: raise ValueError
                except ValueError:
                    send_text("Vui long nhap so tien hop le (1 - 10000 USD)")
                    return jsonify({"ok": True}), 200

                sig      = pending.pop(active_cb, {})
                action   = rs["action"]
                symbol   = sig.get("symbol", "XAUUSD")
                price    = sig.get("price", "—")
                sl, tp   = sig.get("sl"), sig.get("tp")
                tf       = sig.get("tf", "M5")
                emoji    = "🔴" if action == "SELL" else "🟢"

                order_id   = str(uuid.uuid4())[:8]
                mt5_symbol = to_mt5_symbol(symbol)

                mt5_queue[order_id] = {
                    "order_id": order_id, "action": action, "symbol": mt5_symbol,
                    "price": price, "sl": sl, "tp": tp, "risk_usd": risk_usd,
                    "tf": tf, "max_dev": MAX_PRICE_DEV, "created": time.time(),
                    "signal_type": sig.get("signal_type", "vsa"),
                    "trend": sig.get("trend", ""), "mode": sig.get("mode", ""),
                    "harsi": sig.get("harsi", ""),
                }
                del risk_state[active_cb]
                save_state()

                send_text(
                    f"{emoji} <b>DA XAC NHAN {action}</b> — {symbol}\n"
                    f"📍 Entry: {price} | {tf}\n"
                    f"🛑 SL: {sl}\n🎯 TP: {tp}  (RR 1:{DEFAULT_RR})\n"
                    f"💰 Risk: ${risk_usd}\n⏰ {now_ict()}\n"
                    f"⏳ <i>Dang cho EA MT5 thuc thi...</i>"
                )
                return jsonify({"ok": True}), 200

    cq = update.get("callback_query")
    if not cq: return jsonify({"ok": True}), 200

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
                answer_callback(cq_id, "Signal da het han")
                return jsonify({"ok": True}), 200
            answer_callback(cq_id, "Nhap so tien risk toi da (USD)")
            edit_message(chat_id, msg_id,
                f"{'🔴' if action == 'SELL' else '🟢'} <b>XAC NHAN {action}</b> — {sig.get('symbol','XAUUSD')}\n"
                f"📍 Entry: {sig.get('price','—')} | SL: {sig.get('sl','—')} | TP: {sig.get('tp','—')}\n\n"
                f"💰 <b>Reply voi so tien risk toi da (USD)</b>\n<i>Vi du: 50</i>"
            )
            risk_state[callback_id] = {
                "callback_id": callback_id, "chat_id": chat_id,
                "action": action, "waiting_risk": True
            }
            save_state()

    elif parts[0] == "skip" and len(parts) >= 2:
        cb_id = parts[1]
        with lock:
            sig = pending.pop(cb_id, {})
            risk_state.pop(cb_id, None)
            save_state()
        answer_callback(cq_id, "Da bo qua")
        edit_message(chat_id, msg_id,
            f"❌ <b>Da bo qua</b> — {sig.get('symbol','XAUUSD')} {sig.get('tf','')}\n⏰ {now_ict()}")

    return jsonify({"ok": True}), 200


# ═════════════════════════════════════════════════════════════════════════════
# MT5 ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/mt5/pending", methods=["GET"])
def mt5_pending():
    if not verify_api_key(): return jsonify({"error": "unauthorized"}), 401
    with lock:
        request_count["mt5_poll"] += 1
        cleanup()
        if not mt5_queue: return "", 204
        order_id = next(iter(mt5_queue))
        return jsonify(mt5_queue[order_id]), 200


@app.route("/mt5/ack", methods=["POST"])
def mt5_ack():
    if not verify_api_key(): return jsonify({"error": "unauthorized"}), 401

    data     = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id", "")
    status   = data.get("status", "")
    detail   = data.get("detail", "")

    with lock:
        if order_id in mt5_queue:
            sig = mt5_queue.pop(order_id)
            save_state()

            emoji = "✅" if status == "filled" else "❌"
            send_text(
                f"{emoji} <b>MT5: {status.upper()}</b>\n"
                f"📍 {sig.get('action')} {sig.get('symbol')} @ {sig.get('price')}\n"
                f"💰 Risk: ${sig.get('risk_usd', '—')}"
                f"{chr(10) + '📝 ' + detail if detail else ''}\n"
                f"⏰ {now_ict()}"
            )

            if status == "filled":
                sync_sheet(sig, detail, order_id)

                sig_symbol = sig.get("symbol", "").replace(SYMBOL_SUFFIX, "")
                if classify_symbol(sig_symbol) == "crypto":
                    log.info(f"📡 Crypto: skip channel (already posted at /signal)")
                else:
                    try:
                        route_to_channels(sig, detail)
                    except Exception as e:
                        log.error(f"📡 Route error: {e}")
                        if not LEGACY_PUBLIC:
                            try: send_public_signal_legacy(sig, detail)
                            except Exception: pass
        else:
            log.warning(f"Ack for unknown order: {order_id}")

    return jsonify({"ok": True}), 200


@app.route("/debug/last", methods=["GET"])
def debug_last():
    if not verify_api_key(): return jsonify({"error": "unauthorized"}), 401
    return jsonify(last_signal_log), 200

@app.route("/status", methods=["GET"])
def status():
    if not verify_api_key(): return jsonify({"error": "unauthorized"}), 401
    with lock:
        cleanup()
        return jsonify({
            "version": "4.3.1", "uptime_s": int(time.time() - startup_time),
            "requests": request_count, "diag_mode": DIAG_MODE,
            "channel_routing": {
                "xau": XAU_CHANNEL_ID or "(not set)",
                "forex": FOREX_CHANNEL_ID or "(not set)",
                "crypto": (CRYPTO_CHANNEL_ID + " (immediate)") if CRYPTO_CHANNEL_ID else "(not set)",
                "legacy": PUBLIC_CHAT_ID if LEGACY_PUBLIC else "(disabled)",
            },
            "zones": {k: {**v, "expires_in": max(0, int(v["expires"] - time.time()))} for k, v in zone_state.items()},
            "pending": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in pending.items()},
            "mt5_queue": mt5_queue,
            "risk_wait": {k: v for k, v in risk_state.items() if v.get("waiting_risk")},
            "last_signal": last_signal_log,
        }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
