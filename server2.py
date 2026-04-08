import os
import json
import math
import time
import logging
import threading
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
FALLBACK_SL_PCT = float(os.environ.get("FALLBACK_SL_PCT", "8")) / 100

# ─── Trail TP/SL Engine Config ──────────────────────────────
TP_PCT = float(os.environ.get("TP_PCT", "3")) / 100       # 3%
SL_PCT = float(os.environ.get("SL_PCT", "3")) / 100       # 3%
BOOK_PCT = float(os.environ.get("BOOK_PCT", "33")) / 100  # 33% of original qty
MAX_BOOKS = int(os.environ.get("MAX_BOOKS", "2"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))  # seconds

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

def tick_round_sl(sl_price, entry_price, side, symbol):
    """Round SL away from entry (wider)."""
    tick = get_tick_size(symbol)
    if not tick:
        tick = infer_tick_from_price(entry_price)
    if entry_price >= 1.0:
        tick = max(tick, 0.001)
    if side == "buy":
        return round_to_tick(sl_price, tick)    # SL below → round down
    else:
        return round_up_to_tick(sl_price, tick)  # SL above → round up


# ═══════════════════════════════════════════════════════════
#  POSITION TRACKING
# ═══════════════════════════════════════════════════════════
# {symbol: {pair, side, qty, entry_price, original_qty, entry_time,
#           order_id, tp_price, sl_price, books_done, last_hit_tp,
#           leverage, margin_ccy}}
active_trades = {}
trade_lock = threading.Lock()

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

def set_active_trade(pair, side, qty, entry_price, order_id, tp_price=None, sl_price=None, leverage=5, margin_ccy="INR"):
    active_trades[pair] = {
        "pair": pair, "side": side, "qty": qty, "original_qty": qty,
        "entry_price": entry_price, "entry_time": time.time(),
        "order_id": order_id, "tp_price": tp_price, "sl_price": sl_price,
        "books_done": 0, "last_hit_tp": None,
        "leverage": leverage, "margin_ccy": margin_ccy
    }
    log.info(f"📝 Tracked: {side.upper()} {qty} {pair} @ {entry_price} | TP={tp_price} SL={sl_price}")

def clear_active_trade(pair, reason=""):
    old = active_trades.pop(pair, None)
    if old:
        native_sl_orders.pop(pair, None)  # position-attached SL auto-cancels
        log.info(f"🔓 Cleared: {pair} — {reason}")


def calc_quantity(coin_price, leverage):
    if FIXED_MARGIN_INR <= 0:
        return 0
    available_usdt = (FIXED_MARGIN_INR * WALLET_USAGE_PCT) / USDT_INR_RATE
    raw_qty = (available_usdt * leverage) / coin_price
    return round_down_quantity(raw_qty, coin_price)


# ═══════════════════════════════════════════════════════════
#  NATIVE SL — uses CoinDCX create_tpsl endpoint (position-attached)
#  Shows in CoinDCX UI, auto-cancels on position close
# ═══════════════════════════════════════════════════════════
native_sl_orders = {}  # {symbol: {"sl_price": ...}}

def place_native_sl(symbol, side, qty, sl_price, leverage, margin_ccy):
    """Place SL on CoinDCX using create_tpsl endpoint (attached to position)."""
    import threading

    sl_price = tick_round_sl(sl_price, sl_price, side, symbol)

    def _place_sl_worker():
        try:
            # Find position ID
            position_id = _find_position_id(symbol)
            if not position_id:
                log.warning(f"⚠️ Native SL: can't find position for {symbol}")
                return

            body = {
                "id": position_id,
                "stop_loss": {
                    "stop_price": str(sl_price),
                    "order_type": "stop_market"
                }
            }
            resp = _sign_post("/exchange/v1/derivatives/futures/positions/create_tpsl", body)
            result = resp.json() if resp.status_code == 200 else {}

            if isinstance(result, dict) and result.get("status") == "error":
                log.warning(f"⚠️ Native SL failed for {symbol}: {result.get('message', '')}")
                return

            native_sl_orders[symbol] = {"sl_price": sl_price}
            log.info(f"🛡️ Native SL set: {symbol} @ {sl_price} (position-attached)")

        except Exception as e:
            log.warning(f"⚠️ Native SL error for {symbol}: {e}")

    thread = threading.Thread(target=_place_sl_worker, daemon=True)
    thread.start()


def _find_position_id(symbol, max_wait=30, interval=3):
    """Poll for position ID, up to max_wait seconds."""
    elapsed = 0
    while elapsed < max_wait:
        try:
            resp = _sign_post("/exchange/v1/derivatives/futures/positions", {})
            if resp.status_code == 200:
                positions = resp.json()
                if isinstance(positions, list):
                    for p in positions:
                        if p.get("pair") == symbol and abs(float(p.get("quantity", 0))) > 0.0001:
                            return p.get("id")
        except Exception as e:
            log.warning(f"⚠️ Position poll error: {e}")
        time.sleep(interval)
        elapsed += interval
    return None


def cancel_native_sl(symbol):
    """Cancel existing native SL. With create_tpsl, SL is position-attached
    and auto-cancels when position closes. Manual cancel only needed for updates."""
    if symbol not in native_sl_orders:
        return

    try:
        position_id = _find_position_id(symbol, max_wait=5, interval=2)
        if not position_id:
            native_sl_orders.pop(symbol, None)
            return

        # Remove SL by setting it to None via create_tpsl with no stop_loss
        body = {
            "id": position_id,
            "stop_loss": None
        }
        resp = _sign_post("/exchange/v1/derivatives/futures/positions/create_tpsl", body)
        if resp.status_code == 200:
            log.info(f"🛡️ Native SL cleared: {symbol}")
        else:
            log.warning(f"⚠️ Native SL clear may have failed: {symbol}")

    except Exception as e:
        log.warning(f"⚠️ Native SL cancel error for {symbol}: {e}")

    native_sl_orders.pop(symbol, None)


# ═══════════════════════════════════════════════════════════
#  SERVER-SIDE TP/SL ENGINE — polls prices, executes bookings
# ═══════════════════════════════════════════════════════════

def calculate_new_tp_sl(hit_price, side, symbol):
    """Calculate new TP/SL after a TP hit."""
    if side == "buy":
        new_tp = hit_price * (1 + TP_PCT)
        new_sl = hit_price * (1 - SL_PCT)
    else:
        new_tp = hit_price * (1 - TP_PCT)
        new_sl = hit_price * (1 + SL_PCT)
    return new_tp, new_sl


def execute_book(symbol, trade):
    """Execute a partial booking — close BOOK_PCT of original_qty."""
    book_qty = round_down_quantity(trade["original_qty"] * BOOK_PCT, trade["entry_price"])
    book_qty = min(book_qty, trade["qty"])  # safety: never book more than remaining

    if book_qty <= 0:
        log.warning(f"⚠️ Book qty too small for {symbol}, skipping")
        return False

    close_side = "sell" if trade["side"] == "buy" else "buy"
    leverage = trade.get("leverage", DEFAULT_LEVERAGE)
    margin_ccy = trade.get("margin_ccy", DEFAULT_MARGIN)

    log.info(f"📦 SERVER BOOK #{trade['books_done']+1}: closing {book_qty} of {trade['qty']} {symbol}")

    try:
        result = client.place_order(
            pair=symbol, side=close_side, order_type="market_order",
            total_quantity=book_qty, leverage=leverage,
            margin_currency=margin_ccy
        )

        if isinstance(result, dict) and result.get("status") == "error":
            err = result.get("message", "")
            log.error(f"❌ Server book failed: {symbol} — {err}")
            return False

        trade["qty"] -= book_qty
        trade["books_done"] += 1
        trade["last_hit_tp"] = trade["tp_price"]

        # Calculate new TP/SL — SL goes to hit level minus SL_PCT
        old_tp = trade["tp_price"]
        new_tp, new_sl = calculate_new_tp_sl(old_tp, trade["side"], symbol)
        trade["tp_price"] = new_tp
        trade["sl_price"] = new_sl

        log.info(f"✅ Booked {book_qty} {symbol} — remaining: {trade['qty']:.4f} | new TP={new_tp:.6f} SL={new_sl:.6f}")
        log_trade_event(symbol, close_side, "server_book", "FILLED", f"book #{trade['books_done']}, remaining={trade['qty']:.4f}")

        # Re-place native SL at new level with remaining qty
        if trade["qty"] > 0:
            place_native_sl(symbol, trade["side"], trade["qty"], trade["sl_price"], leverage, margin_ccy)

        return True

    except Exception as e:
        log.error(f"❌ Server book error for {symbol}: {e}")
        return False


def execute_trail_shift(symbol, trade):
    """Trail mode — no booking, just shift TP/SL."""
    old_tp = trade["tp_price"]
    leverage = trade.get("leverage", DEFAULT_LEVERAGE)
    margin_ccy = trade.get("margin_ccy", DEFAULT_MARGIN)

    # SL moves to the PREVIOUS TP hit (one step back)
    new_sl = trade["last_hit_tp"]
    trade["last_hit_tp"] = old_tp  # current hit becomes the new "previous"

    if trade["side"] == "buy":
        new_tp = old_tp * (1 + TP_PCT)
    else:
        new_tp = old_tp * (1 - TP_PCT)

    trade["tp_price"] = new_tp
    trade["sl_price"] = new_sl

    log.info(f"🔄 SERVER TRAIL: {symbol} | new TP={new_tp:.6f} SL={new_sl:.6f}")
    log_trade_event(symbol, "", "server_trail", "SHIFTED", f"TP={new_tp:.6f} SL={new_sl:.6f}")

    # Update native SL on CoinDCX (create_tpsl overwrites existing)
    place_native_sl(symbol, trade["side"], trade["qty"], new_sl, leverage, margin_ccy)


def execute_sl_close(symbol, trade):
    """SL hit — close all remaining qty."""
    close_side = "sell" if trade["side"] == "buy" else "buy"
    close_qty = trade["qty"]
    leverage = trade.get("leverage", DEFAULT_LEVERAGE)
    margin_ccy = trade.get("margin_ccy", DEFAULT_MARGIN)

    # Native SL auto-cancels when position closes — no manual cancel needed
    native_sl_orders.pop(symbol, None)

    if close_qty <= 0:
        clear_active_trade(symbol, "SL hit — qty already 0")
        return

    log.info(f"🔻 SERVER SL HIT: {close_side.upper()} {close_qty} {symbol}")

    try:
        result = client.place_order(
            pair=symbol, side=close_side, order_type="market_order",
            total_quantity=close_qty, leverage=leverage,
            margin_currency=margin_ccy
        )

        if isinstance(result, dict) and result.get("status") == "error":
            err = result.get("message", "")
            log.warning(f"⚠️ SL close may have failed (likely already closed by native SL): {err}")

        log_trade_event(symbol, close_side, "server_sl", "FILLED", f"closed {close_qty}")

    except Exception as e:
        log.error(f"❌ SL close error for {symbol}: {e}")

    clear_active_trade(symbol, "server SL hit — waiting for Pine reverse")


def get_current_prices():
    """Fetch current mark prices for all tracked positions from CoinDCX."""
    try:
        resp = _sign_post("/exchange/v1/derivatives/futures/positions", {})
        if resp.status_code != 200:
            return {}

        positions = resp.json()
        if not isinstance(positions, list):
            return {}

        prices = {}
        open_on_exchange = set()
        for p in positions:
            pair = p.get("pair", "")
            qty = abs(float(p.get("quantity", 0)))
            mark = float(p.get("mark_price", 0) or p.get("avg_price", 0) or 0)
            if qty > 0 and mark > 0:
                prices[pair] = mark
                open_on_exchange.add(pair)

        # Ghost detection — positions tracked but no longer on exchange
        with trade_lock:
            if open_on_exchange:  # only if API returned at least 1 position
                ghosts = [sym for sym in active_trades if sym not in open_on_exchange]
                for sym in ghosts:
                    log.info(f"👻 SYNC: {sym} no longer open on CoinDCX — clearing")
                    log_trade_event(sym, "", "sync", "CLEARED", "position closed on CoinDCX")
                    clear_active_trade(sym, "synced — closed on CoinDCX")
                if ghosts:
                    log.info(f"🔄 SYNC: cleared {len(ghosts)} ghost slots, {len(active_trades)} remain")

        return prices

    except Exception as e:
        log.warning(f"⚠️ Price fetch failed: {e}")
        return {}


def price_monitor_loop():
    """Background thread — polls prices every POLL_INTERVAL seconds,
    executes bookings / trail shifts / SL closes."""
    log.info(f"📡 Price monitor started — polling every {POLL_INTERVAL}s")

    while True:
        try:
            time.sleep(POLL_INTERVAL)

            if not active_trades:
                continue

            prices = get_current_prices()
            if not prices:
                continue

            # Work on a snapshot of symbols to avoid mutation during iteration
            with trade_lock:
                symbols = list(active_trades.keys())

            for symbol in symbols:
                with trade_lock:
                    if symbol not in active_trades:
                        continue
                    trade = active_trades[symbol]

                price = prices.get(symbol)
                if not price:
                    continue

                tp = trade["tp_price"]
                sl = trade["sl_price"]
                side = trade["side"]

                if not tp or not sl:
                    continue

                # ── Check TP ──
                tp_hit = False
                if side == "buy" and price >= tp:
                    tp_hit = True
                elif side == "sell" and price <= tp:
                    tp_hit = True

                if tp_hit:
                    with trade_lock:
                        if symbol not in active_trades:
                            continue
                        trade = active_trades[symbol]
                        if trade["books_done"] < MAX_BOOKS:
                            execute_book(symbol, trade)
                        else:
                            execute_trail_shift(symbol, trade)
                    continue  # re-check on next poll after TP action

                # ── Check SL ──
                sl_hit = False
                if side == "buy" and price <= sl:
                    sl_hit = True
                elif side == "sell" and price >= sl:
                    sl_hit = True

                if sl_hit:
                    with trade_lock:
                        if symbol not in active_trades:
                            continue
                        trade = active_trades[symbol]
                        execute_sl_close(symbol, trade)

        except Exception as e:
            log.error(f"❌ Price monitor error: {e}", exc_info=True)


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

        action = data.get("action", "").lower()
        symbol = data.get("symbol", "")
        alert_type = data.get("type", "entry").lower()
        leverage = int(data.get("leverage", DEFAULT_LEVERAGE))
        margin_ccy = data.get("margin_currency", DEFAULT_MARGIN)
        coin_price = float(data.get("price", 0))

        tp_price = float(data.get("tp_price", 0)) if data.get("tp_price") else None
        sl_price = float(data.get("sl_price", 0)) if data.get("sl_price") else None

        if action not in ("buy", "sell"):
            return jsonify({"error": "invalid action", "status": "rejected"}), 200
        if not symbol:
            return jsonify({"error": "missing symbol", "status": "rejected"}), 200

        # ─── ENTRY — initial position ─────────────────────────
        if alert_type == "entry":
            with trade_lock:
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
                return jsonify({"status": "rejected", "reason": "qty=0"}), 200

            # Calculate TP/SL if not provided by Pine
            if not tp_price:
                tp_price = coin_price * (1 + TP_PCT) if action == "buy" else coin_price * (1 - TP_PCT)
            if not sl_price:
                sl_price = coin_price * (1 - SL_PCT) if action == "buy" else coin_price * (1 + SL_PCT)

            log.info(f"🚀 ENTRY {action.upper()} {quantity} {symbol} ({leverage}x) | TP={tp_price:.6f} SL={sl_price:.6f}")
            result = client.place_order(
                pair=symbol, side=action, order_type="market_order",
                total_quantity=quantity, leverage=leverage,
                margin_currency=margin_ccy
            )

            if isinstance(result, dict) and result.get("status") == "error":
                err = result.get("message", "")
                log.error(f"❌ REJECT: {symbol} — {err}")
                log_trade_event(symbol, action, "entry", "REJECT", err)
                return jsonify({"status": "rejected", "reason": err}), 200

            order_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
            filled_qty = float(result.get("total_quantity", quantity)) if isinstance(result, dict) else quantity

            with trade_lock:
                set_active_trade(symbol, action, filled_qty, coin_price, order_id,
                                 tp_price=tp_price, sl_price=sl_price,
                                 leverage=leverage, margin_ccy=margin_ccy)

            log_trade_event(symbol, action, "entry", "FILLED", f"TP={tp_price:.6f} SL={sl_price:.6f}")

            # Place native SL on CoinDCX
            place_native_sl(symbol, action, filled_qty, sl_price, leverage, margin_ccy)

            return jsonify({"status": "success", "order": result}), 200

        # ─── BOOK — Pine sends book, but server may have already handled it ──
        elif alert_type == "book":
            with trade_lock:
                if symbol not in active_trades:
                    log.info(f"⚠️ SKIP book: {symbol} not tracked (server may have handled it)")
                    return jsonify({"status": "skipped", "reason": "not tracked or already booked"}), 200

                trade = active_trades[symbol]

                # Update TP/SL from Pine if provided (sync)
                if tp_price:
                    trade["tp_price"] = tp_price
                if sl_price:
                    trade["sl_price"] = sl_price

            log.info(f"📦 Pine book received for {symbol} — server is managing bookings, syncing TP/SL")
            return jsonify({"status": "synced"}), 200

        # ─── REVERSE — SL hit, close remaining + open opposite ──
        elif alert_type == "reverse":
            with trade_lock:
                # Position-attached SL auto-cancels on close
                native_sl_orders.pop(symbol, None)

                if symbol in active_trades:
                    trade = active_trades[symbol]
                    close_side = "sell" if trade["side"] == "buy" else "buy"
                    close_qty = trade["qty"]

                    if close_qty > 0:
                        log.info(f"🔻 REVERSE close: {close_side.upper()} {close_qty} {symbol}")
                        close_result = client.place_order(
                            pair=symbol, side=close_side, order_type="market_order",
                            total_quantity=close_qty, leverage=trade.get("leverage", leverage),
                            margin_currency=trade.get("margin_ccy", margin_ccy)
                        )
                        if isinstance(close_result, dict) and close_result.get("status") == "error":
                            err = close_result.get("message", "")
                            log.warning(f"⚠️ Reverse close failed (likely already closed by server): {err}")

                    clear_active_trade(symbol, "reverse — Pine SL/reverse")
                    log_trade_event(symbol, close_side, "reverse_close", "FILLED", "Pine reverse")

            time.sleep(1)

            # Open new position in opposite direction
            quantity = calc_quantity(coin_price, leverage)
            if quantity <= 0:
                log.error(f"❌ REJECT reverse entry: {symbol} — qty=0")
                return jsonify({"status": "rejected", "reason": "reverse entry qty=0"}), 200

            # Calculate TP/SL for new position
            if not tp_price:
                tp_price = coin_price * (1 + TP_PCT) if action == "buy" else coin_price * (1 - TP_PCT)
            if not sl_price:
                sl_price = coin_price * (1 - SL_PCT) if action == "buy" else coin_price * (1 + SL_PCT)

            log.info(f"🔄 REVERSE entry: {action.upper()} {quantity} {symbol} | TP={tp_price:.6f} SL={sl_price:.6f}")
            result = client.place_order(
                pair=symbol, side=action, order_type="market_order",
                total_quantity=quantity, leverage=leverage,
                margin_currency=margin_ccy
            )

            if isinstance(result, dict) and result.get("status") == "error":
                err = result.get("message", "")
                log.error(f"❌ Reverse entry failed: {err}")
                log_trade_event(symbol, action, "reverse_entry", "REJECT", err)
                return jsonify({"status": "rejected", "reason": err}), 200

            order_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
            filled_qty = float(result.get("total_quantity", quantity)) if isinstance(result, dict) else quantity

            with trade_lock:
                set_active_trade(symbol, action, filled_qty, coin_price, order_id,
                                 tp_price=tp_price, sl_price=sl_price,
                                 leverage=leverage, margin_ccy=margin_ccy)

            log_trade_event(symbol, action, "reverse_entry", "FILLED", f"TP={tp_price:.6f} SL={sl_price:.6f}")

            # Place native SL for new position
            place_native_sl(symbol, action, filled_qty, sl_price, leverage, margin_ccy)

            return jsonify({"status": "reversed", "order": result}), 200

        # ─── CLOSE — kill switch ──────────────────────────────
        elif alert_type == "close":
            reason = data.get("reason", "unknown")
            ret_pct = data.get("return_pct", "?")
            log.info(f"☠️ KILL SWITCH: {symbol} — reason={reason}, return={ret_pct}%")

            with trade_lock:
                native_sl_orders.pop(symbol, None)

                if symbol in active_trades:
                    trade = active_trades[symbol]
                    close_qty = trade["qty"]
                    close_side = "sell" if trade["side"] == "buy" else "buy"

                    if close_qty > 0:
                        log.info(f"🔻 KILL close: {close_side.upper()} {close_qty} {symbol}")
                        result = client.place_order(
                            pair=symbol, side=close_side, order_type="market_order",
                            total_quantity=close_qty, leverage=trade.get("leverage", leverage),
                            margin_currency=trade.get("margin_ccy", margin_ccy)
                        )
                        if isinstance(result, dict) and result.get("status") == "error":
                            log.warning(f"⚠️ Kill close may have failed: {result.get('message','')}")

                    clear_active_trade(symbol, f"kill switch — return {ret_pct}%")
                    log_trade_event(symbol, close_side, "kill", "FILLED", f"return={ret_pct}%, reason={reason}")
                else:
                    log.info(f"⚠️ Kill for {symbol} but not tracked — no action needed")
                    log_trade_event(symbol, action, "kill", "SKIP", "not tracked")

            return jsonify({"status": "killed", "symbol": symbol, "return_pct": ret_pct}), 200

        else:
            return jsonify({"status": "unknown_type", "type": alert_type}), 200

    except Exception as e:
        log.error(f"❌ Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 200  # always 200 to TradingView


# ═══════════════════════════════════════════════════════════
#  UTILITY ENDPOINTS
# ═══════════════════════════════════════════════════════════
@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "active_trades": active_trades,
        "native_sl_orders": native_sl_orders,
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
        "config": {
            "tp_pct": TP_PCT * 100, "sl_pct": SL_PCT * 100,
            "book_pct": BOOK_PCT * 100, "max_books": MAX_BOOKS,
            "poll_interval": POLL_INTERVAL
        },
        "time": datetime.now().isoformat()
    })

@app.route("/clear-lock", methods=["POST"])
def clear_lock():
    symbol = request.args.get("symbol")
    with trade_lock:
        if symbol:
            clear_active_trade(symbol, "manual clear")
            return jsonify({"status": "ok", "cleared": symbol})
        else:
            native_sl_orders.clear()
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

                        # Calculate TP/SL from entry price
                        if side == "buy":
                            tp = entry_price * (1 + TP_PCT)
                            sl = entry_price * (1 - SL_PCT)
                        else:
                            tp = entry_price * (1 - TP_PCT)
                            sl = entry_price * (1 + SL_PCT)

                        active_trades[pair] = {
                            "pair": pair, "side": side, "qty": qty, "original_qty": qty,
                            "entry_price": entry_price, "entry_time": time.time(),
                            "order_id": "recovered", "tp_price": tp, "sl_price": sl,
                            "books_done": 0, "last_hit_tp": None,
                            "leverage": DEFAULT_LEVERAGE, "margin_ccy": DEFAULT_MARGIN
                        }
                        log.info(f"🔄 Recovered: {side.upper()} {qty} {pair} @ {entry_price} | TP={tp:.6f} SL={sl:.6f}")

                        # Place native SL for recovered position
                        place_native_sl(pair, side, qty, sl, DEFAULT_LEVERAGE, DEFAULT_MARGIN)

        log.info(f"🔄 Recovery done: {len(active_trades)} positions")
    except Exception as e:
        log.warning(f"⚠️ Recovery error: {e}")


load_tick_sizes()
recover_active_trades()

# Start price monitor in background thread
monitor_thread = threading.Thread(target=price_monitor_loop, daemon=True)
monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"🤖 Trail TP/SL Reversal Bot v2 starting on port {port}")
    log.info(f"📊 Config: TP={TP_PCT*100:.1f}% SL={SL_PCT*100:.1f}% Book={BOOK_PCT*100:.0f}% MaxBooks={MAX_BOOKS} Poll={POLL_INTERVAL}s")
    app.run(host="0.0.0.0", port=port)
