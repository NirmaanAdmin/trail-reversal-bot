import os
import json
import math
import time
import logging
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from coindcx_client import CoinDCXFutures

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

API_KEY = os.environ.get("COINDCX_API_KEY", "")
API_SECRET = os.environ.get("COINDCX_API_SECRET", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEFAULT_LEVERAGE = int(os.environ.get("DEFAULT_LEVERAGE", "5"))
DEFAULT_MARGIN = os.environ.get("DEFAULT_MARGIN", "INR")

app = Flask(__name__)
client = CoinDCXFutures(API_KEY, API_SECRET)

# ─── Signed POST helper ──────────────────────────────────────
import requests as _req
import hmac as _hmac
import hashlib as _hashlib

def _sign_post(endpoint, body):
    body["timestamp"] = int(round(time.time() * 1000))
    json_body = json.dumps(body, separators=(",", ":"))
    sig = _hmac.new(API_SECRET.encode(), json_body.encode(), _hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-AUTH-APIKEY": API_KEY, "X-AUTH-SIGNATURE": sig}
    return _req.post(f"https://api.coindcx.com{endpoint}", data=json_body, headers=headers, timeout=15)

# ─── Tick Size Cache ──────────────────────────────────────────
tick_cache = {}
tick_cache_time = 0
TICK_CACHE_TTL = 3600

def load_tick_sizes():
    global tick_cache, tick_cache_time
    try:
        resp = _req.get("https://api.coindcx.com/exchange/v1/markets_details", timeout=15)
        data = resp.json()
        for m in data:
            pair = m.get("pair", "")
            prec = m.get("base_currency_precision")
            if prec is None:
                continue
            tick = 10 ** (-int(prec))
            if pair.startswith("B-"):
                tick_cache[pair] = tick
            elif pair.startswith("KC-") and pair.endswith("_USDT"):
                b_pair = "B-" + pair[3:]
                if b_pair not in tick_cache:
                    tick_cache[b_pair] = tick
        tick_cache_time = time.time()
        log.info(f"📊 Loaded tick sizes for {len(tick_cache)} futures pairs")
    except Exception as e:
        log.error(f"❌ Failed to load tick sizes: {e}")

def get_tick_size(symbol):
    global tick_cache_time
    if time.time() - tick_cache_time > TICK_CACHE_TTL or not tick_cache:
        load_tick_sizes()
    return tick_cache.get(symbol)

def round_to_tick(price, tick_size):
    if tick_size is None or tick_size <= 0:
        return price
    decimals = max(0, -math.floor(math.log10(tick_size)))
    return round(math.floor(price / tick_size) * tick_size, decimals)

def round_up_to_tick(price, tick_size):
    if tick_size is None or tick_size <= 0:
        return price
    decimals = max(0, -math.floor(math.log10(tick_size)))
    return round(math.ceil(price / tick_size) * tick_size, decimals)

def round_down_quantity(qty, price):
    if price >= 1000:
        return math.floor(qty * 1000) / 1000
    elif price >= 100:
        return math.floor(qty * 100) / 100
    elif price >= 10:
        return math.floor(qty * 10) / 10
    else:
        return math.floor(qty)

def infer_tick_from_price(price):
    price_str = f"{price:.10f}".rstrip('0')
    decimals = len(price_str.split('.')[1]) if '.' in price_str else 0
    return 10 ** (-max(1, min(decimals, 8)))

def round_tp_sl(entry_price, tp, sl, side, symbol):
    """Round TP/SL to tick size, TP away from entry, SL away from entry."""
    tick = get_tick_size(symbol)
    if not tick:
        tick = infer_tick_from_price(entry_price)
    if entry_price >= 1.0:
        tick = max(tick, 0.001)
    if side == "buy":
        tp = round_up_to_tick(tp, tick)
        sl = round_to_tick(sl, tick)
    else:
        tp = round_to_tick(tp, tick)
        sl = round_up_to_tick(sl, tick)
    return tp, sl


# ═══════════════════════════════════════════════════════════
#  POSITION TRACKING
# ═══════════════════════════════════════════════════════════
# {symbol: {pair, side, qty, entry_price, entry_time, order_id,
#           tp_price, sl_price, books_done, position_id}}
active_trades = {}

FIXED_MARGIN_INR = float(os.environ.get("FIXED_MARGIN_INR", "0"))
USDT_INR_RATE = float(os.environ.get("USDT_INR_RATE", "98"))
WALLET_USAGE_PCT = float(os.environ.get("WALLET_USAGE_PCT", "100")) / 100

# ─── Event Log ───
trade_log = []
MAX_LOG = 50

def log_trade_event(symbol, action, alert_type, result, reason=""):
    trade_log.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": symbol, "action": action,
        "type": alert_type, "result": result, "reason": reason
    })
    if len(trade_log) > MAX_LOG:
        trade_log.pop(0)

def set_active_trade(pair, side, qty, entry_price, order_id, tp_price=None, sl_price=None):
    active_trades[pair] = {
        "pair": pair, "side": side, "qty": qty,
        "entry_price": entry_price, "entry_time": time.time(),
        "order_id": order_id, "tp_price": tp_price, "sl_price": sl_price,
        "books_done": 0
    }
    log.info(f"📝 Tracked: {side.upper()} {qty} {pair} @ {entry_price} | TP={tp_price} SL={sl_price}")

def clear_active_trade(pair, reason=""):
    old = active_trades.pop(pair, None)
    if old:
        log.info(f"🔓 Cleared: {pair} — {reason}")

def calc_quantity(coin_price, leverage):
    """Calculate quantity from FIXED_MARGIN_INR."""
    if FIXED_MARGIN_INR <= 0:
        return 0
    available_usdt = (FIXED_MARGIN_INR * WALLET_USAGE_PCT) / USDT_INR_RATE
    raw_qty = (available_usdt * leverage) / coin_price
    return round_down_quantity(raw_qty, coin_price)


# ═══════════════════════════════════════════════════════════
#  POSITION SYNC — auto-detect manual closes on CoinDCX
# ═══════════════════════════════════════════════════════════
_last_sync_time = 0
SYNC_INTERVAL = 30

def sync_positions():
    """Check CoinDCX for actual open positions. Clear any tracked positions that are already closed.
    SAFETY: Only clears ghosts when API returns at least 1 position (proves API is working).
    If API returns empty but we track positions, skip — API may be unreliable."""
    global _last_sync_time

    now = time.time()
    if now - _last_sync_time < SYNC_INTERVAL:
        return
    _last_sync_time = now

    if not active_trades:
        return

    try:
        resp = _sign_post("/exchange/v1/derivatives/futures/positions", {})
        if resp.status_code != 200:
            return

        positions = resp.json()
        if not isinstance(positions, list):
            return

        open_on_exchange = set()
        for p in positions:
            if abs(float(p.get("quantity", 0))) > 0:
                open_on_exchange.add(p.get("pair", ""))

        # SAFETY: If API returns NO positions but we track some, don't trust it
        if not open_on_exchange and len(active_trades) > 0:
            return

        # Find ghost positions — tracked by server but not on CoinDCX
        ghosts = [sym for sym in active_trades if sym not in open_on_exchange]

        for sym in ghosts:
            log.info(f"👻 SYNC: {sym} no longer open on CoinDCX — clearing (manual close or TP/SL hit)")
            log_trade_event(sym, "", "sync", "CLEARED", "position closed on CoinDCX")
            clear_active_trade(sym, "synced — closed on CoinDCX")

        if ghosts:
            log.info(f"🔄 SYNC: cleared {len(ghosts)} ghost slots, {len(active_trades)} remain")

    except Exception as e:
        log.warning(f"⚠️ SYNC: positions check failed: {e}")


# ═══════════════════════════════════════════════════════════
#  WEBHOOK HANDLER
# ═══════════════════════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json or json.loads(request.data.decode("utf-8"))
        log.info(f"📩 Webhook: {json.dumps(data)}")

        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        # Sync positions — detect manual closes on CoinDCX
        sync_positions()

        action = data.get("action", "").lower()       # buy / sell
        symbol = data.get("symbol", "")
        alert_type = data.get("type", "entry").lower() # entry / book / reverse
        leverage = int(data.get("leverage", DEFAULT_LEVERAGE))
        margin_ccy = data.get("margin_currency", DEFAULT_MARGIN)
        coin_price = float(data.get("price", 0))

        # Pine sends these for book/reverse
        tp_price = float(data.get("tp_price", 0)) if data.get("tp_price") else None
        sl_price = float(data.get("sl_price", 0)) if data.get("sl_price") else None
        book_pct = float(data.get("book_pct", 33)) / 100  # % of qty to book

        if action not in ("buy", "sell"):
            return jsonify({"error": "invalid action"}), 400
        if not symbol:
            return jsonify({"error": "missing symbol"}), 400

        # ─── ENTRY — initial position or reversal new leg ───────
        if alert_type == "entry":
            # Block same symbol re-entry
            if symbol in active_trades:
                trade = active_trades[symbol]
                mins = int((time.time() - trade["entry_time"]) // 60)
                log.info(f"🚫 SKIP entry: {symbol} already active ({mins}m, {trade['side']})")
                log_trade_event(symbol, action, "entry", "SKIP", f"already active ({mins}m)")
                return jsonify({"status": "skipped", "reason": "already active"}), 200

            quantity = calc_quantity(coin_price, leverage)
            if quantity <= 0:
                log.error(f"❌ REJECT: {symbol} — qty=0")
                log_trade_event(symbol, action, "entry", "REJECT", "qty=0")
                return jsonify({"error": "invalid quantity"}), 400

            # No TP/SL on entry — Pine handles all exits (book/reverse/kill)
            log.info(f"🚀 ENTRY {action.upper()} {quantity} {symbol} ({leverage}x) | NO TP/SL (Pine-driven)")
            result = client.place_order(
                pair=symbol, side=action, order_type="market_order",
                total_quantity=quantity, leverage=leverage,
                margin_currency=margin_ccy
            )

            if isinstance(result, dict) and result.get("status") == "error":
                err = result.get("message", "")
                log.error(f"❌ REJECT: {symbol} — {err}")
                log_trade_event(symbol, action, "entry", "REJECT", err)
                return jsonify(result), 400

            order_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
            filled_qty = float(result.get("total_quantity", quantity)) if isinstance(result, dict) else quantity
            set_active_trade(symbol, action, filled_qty, coin_price, order_id)
            log_trade_event(symbol, action, "entry", "FILLED", "Pine-driven, no TP/SL")
            return jsonify({"status": "success", "order": result}), 200

        # ─── BOOK — partial TP booking + trail TP/SL ────────────
        elif alert_type == "book":
            if symbol not in active_trades:
                log.info(f"⚠️ SKIP book: {symbol} not tracked")
                log_trade_event(symbol, action, "book", "SKIP", "not tracked")
                return jsonify({"status": "skipped"}), 200

            trade = active_trades[symbol]
            book_qty = round_down_quantity(trade["qty"] * book_pct, coin_price)
            if book_qty <= 0:
                log.info(f"⚠️ SKIP book: {symbol} — book qty too small")
                return jsonify({"status": "skipped", "reason": "book qty too small"}), 200

            # Close partial qty at market (opposite side to close)
            close_side = "sell" if trade["side"] == "buy" else "buy"
            log.info(f"📦 BOOK #{trade['books_done']+1}: closing {book_qty} of {trade['qty']} {symbol}")

            result = client.place_order(
                pair=symbol, side=close_side, order_type="market_order",
                total_quantity=book_qty, leverage=leverage,
                margin_currency=margin_ccy
            )

            if isinstance(result, dict) and result.get("status") == "error":
                err = result.get("message", "")
                log.error(f"❌ Book failed: {symbol} — {err}")
                log_trade_event(symbol, action, "book", "REJECT", err)
                return jsonify(result), 400

            # Update tracked qty
            trade["qty"] -= book_qty
            trade["books_done"] += 1
            log.info(f"✅ Booked {book_qty} {symbol} — remaining qty: {trade['qty']}")

            log_trade_event(symbol, action, "book", "FILLED", f"book #{trade['books_done']}, remaining={trade['qty']:.4f}")
            return jsonify({"status": "booked", "remaining_qty": trade["qty"]}), 200

        # ─── REVERSE — SL hit, close remaining + open opposite ──
        elif alert_type == "reverse":
            if symbol in active_trades:
                trade = active_trades[symbol]
                close_side = "sell" if trade["side"] == "buy" else "buy"
                close_qty = trade["qty"]

                if close_qty > 0:
                    log.info(f"🔻 REVERSE close: {close_side.upper()} {close_qty} {symbol}")
                    close_result = client.place_order(
                        pair=symbol, side=close_side, order_type="market_order",
                        total_quantity=close_qty, leverage=leverage,
                        margin_currency=margin_ccy
                    )
                    if isinstance(close_result, dict) and close_result.get("status") == "error":
                        err = close_result.get("message", "")
                        # Position likely already closed by CoinDCX TP/SL
                        log.warning(f"⚠️ Reverse close failed (likely already closed): {err}")

                clear_active_trade(symbol, "reverse — SL hit")
                log_trade_event(symbol, close_side, "reverse_close", "FILLED", "SL hit — closing")

            # Small delay for CoinDCX to settle
            time.sleep(1)

            # Open new position in opposite direction — no TP/SL, Pine drives exits
            quantity = calc_quantity(coin_price, leverage)
            if quantity <= 0:
                log.error(f"❌ REJECT reverse entry: {symbol} — qty=0")
                return jsonify({"error": "reverse entry qty=0"}), 400

            log.info(f"🔄 REVERSE entry: {action.upper()} {quantity} {symbol} | NO TP/SL (Pine-driven)")
            result = client.place_order(
                pair=symbol, side=action, order_type="market_order",
                total_quantity=quantity, leverage=leverage,
                margin_currency=margin_ccy
            )

            if isinstance(result, dict) and result.get("status") == "error":
                err = result.get("message", "")
                log.error(f"❌ Reverse entry failed: {err}")
                log_trade_event(symbol, action, "reverse_entry", "REJECT", err)
                return jsonify(result), 400

            order_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
            filled_qty = float(result.get("total_quantity", quantity)) if isinstance(result, dict) else quantity
            set_active_trade(symbol, action, filled_qty, coin_price, order_id)
            log_trade_event(symbol, action, "reverse_entry", "FILLED", "Pine-driven, no TP/SL")
            return jsonify({"status": "reversed", "order": result}), 200

        # ─── CLOSE — kill switch, close position and free slot ──
        elif alert_type == "close":
            reason = data.get("reason", "unknown")
            ret_pct = data.get("return_pct", "?")
            log.info(f"☠️ KILL SWITCH: {symbol} — reason={reason}, return={ret_pct}%")

            if symbol in active_trades:
                trade = active_trades[symbol]
                close_qty = trade["qty"]
                close_side = "sell" if trade["side"] == "buy" else "buy"

                if close_qty > 0:
                    log.info(f"🔻 KILL close: {close_side.upper()} {close_qty} {symbol}")
                    result = client.place_order(
                        pair=symbol, side=close_side, order_type="market_order",
                        total_quantity=close_qty, leverage=leverage,
                        margin_currency=margin_ccy
                    )
                    if isinstance(result, dict) and result.get("status") == "error":
                        log.warning(f"⚠️ Kill close may have failed (likely already closed): {result.get('message','')}")

                clear_active_trade(symbol, f"kill switch — return {ret_pct}%")
                log_trade_event(symbol, close_side, "kill", "FILLED", f"return={ret_pct}%, reason={reason}")
            else:
                log.info(f"⚠️ Kill for {symbol} but not tracked — no action needed")
                log_trade_event(symbol, action, "kill", "SKIP", "not tracked")

            return jsonify({"status": "killed", "symbol": symbol, "return_pct": ret_pct}), 200

        else:
            return jsonify({"error": f"unknown type: {alert_type}"}), 400

    except Exception as e:
        log.error(f"❌ Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  UTILITY ENDPOINTS
# ═══════════════════════════════════════════════════════════
@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "active_trades": active_trades,
        "positions": len(active_trades),
        "time": datetime.now().isoformat()
    })

@app.route("/stats", methods=["GET"])
def stats():
    filled = [e for e in trade_log if e["result"] == "FILLED"]
    skipped = [e for e in trade_log if e["result"] == "SKIP"]
    rejected = [e for e in trade_log if e["result"] == "REJECT"]
    return jsonify({
        "active_trades": active_trades,
        "positions": len(active_trades),
        "summary": {"filled": len(filled), "skipped": len(skipped), "rejected": len(rejected)},
        "recent_events": list(reversed(trade_log[-20:])),
        "time": datetime.now().isoformat()
    })

@app.route("/clear-lock", methods=["POST"])
def clear_lock():
    symbol = request.args.get("symbol")
    if symbol:
        clear_active_trade(symbol, "manual clear")
        return jsonify({"status": "ok", "cleared": symbol})
    else:
        count = len(active_trades)
        active_trades.clear()
        return jsonify({"status": "ok", "cleared": count})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok", "positions": len(active_trades),
        "active": list(active_trades.keys()),
        "time": datetime.now().isoformat()
    })

# ─── Recover on startup ──────────────────────────────────────
def recover_active_trades():
    try:
        resp = _sign_post("/exchange/v1/derivatives/futures/positions", {})
        if resp.status_code == 200:
            positions = resp.json()
            if isinstance(positions, list):
                for pos in positions:
                    if abs(float(pos.get("quantity", 0))) > 0:
                        pair = pos.get("pair", "")
                        qty = abs(float(pos.get("quantity", 0)))
                        entry_price = float(pos.get("avg_price", 0) or pos.get("entry_price", 0) or 0)
                        side = "buy" if float(pos.get("quantity", 0)) > 0 else "sell"
                        active_trades[pair] = {
                            "pair": pair, "side": side, "qty": qty,
                            "entry_price": entry_price, "entry_time": time.time(),
                            "order_id": "recovered", "tp_price": None, "sl_price": None,
                            "books_done": 0
                        }
                        log.info(f"🔄 Recovered: {side.upper()} {qty} {pair} @ {entry_price}")
        log.info(f"🔄 Recovery done: {len(active_trades)} positions")
    except Exception as e:
        log.warning(f"⚠️ Recovery error: {e}")

load_tick_sizes()
recover_active_trades()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"🤖 Trailing TP/SL Reversal Bot starting on port {port}")
    app.run(host="0.0.0.0", port=port)
