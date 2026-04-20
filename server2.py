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
    """Round SL away from entry."""
    tick = get_tick_size(symbol)
    if not tick:
        tick = infer_tick_from_price(entry_price)
    if entry_price >= 1.0:
        tick = max(tick, 0.001)
    if side == "buy":
        return round_to_tick(sl_price, tick)
    else:
        return round_up_to_tick(sl_price, tick)


# ═══════════════════════════════════════════════════════════
#  POSITION TRACKING
# ═══════════════════════════════════════════════════════════
# {symbol: {pair, side, qty, original_qty, entry_price, entry_time,
#           order_id, tp_price, sl_price, books_done, leverage, margin_ccy}}
active_trades = {}

# ─── Entry cooldown safety net ─────────────────────────────
# Blocks stray `entry` alerts within N seconds of a successful entry/reverse.
# Independent of active_trades — catches cases where in-memory tracking is
# somehow lost but Pine still fires a redundant entry webhook.
last_action_time = {}  # {symbol: epoch_seconds}
ENTRY_COOLDOWN_SEC = int(os.environ.get("ENTRY_COOLDOWN_SEC", "60"))

# ─── Fill Quality Logging ──────────────────────────────────
# Logs the actual CoinDCX order response after each place_order call.
# Flags partial fills / non-200 statuses as WARNING so they surface in Railway logs.
# Does NOT change any existing behavior — purely observational.
def log_order_response(symbol, side, requested_qty, result, context=""):
    try:
        if not isinstance(result, dict):
            log.warning(f"⚠️ FILL [{context}] {symbol} {side}: non-dict response "
                        f"type={type(result).__name__} value={str(result)[:200]}")
            return

        total_qty     = result.get("total_quantity")
        remaining_qty = result.get("remaining_quantity")
        filled_qty    = result.get("filled_quantity")
        status        = result.get("status")
        avg_price     = result.get("avg_price") or result.get("average_price")
        order_id      = result.get("id", "?")

        # Derive filled_qty if CoinDCX only gave us total + remaining
        if filled_qty is None and total_qty is not None and remaining_qty is not None:
            try:
                filled_qty = float(total_qty) - float(remaining_qty)
            except (TypeError, ValueError):
                pass

        log.info(f"📊 FILL [{context}] {symbol} {side} id={order_id} "
                 f"req={requested_qty} total={total_qty} filled={filled_qty} "
                 f"remaining={remaining_qty} status={status} avg={avg_price}")

        # Partial-fill detector (>1% shortfall)
        try:
            if filled_qty is not None:
                f = float(filled_qty)
                r = float(requested_qty)
                if r > 0 and (r - f) / r > 0.01:
                    pct = (f / r * 100) if r > 0 else 0
                    log.warning(f"⚠️ PARTIAL FILL [{context}] {symbol} {side}: "
                                f"requested={r} filled={f} ({pct:.1f}%) — "
                                f"active_trades may now differ from CoinDCX reality")
        except (TypeError, ValueError):
            pass
    except Exception as e:
        log.error(f"❌ log_order_response crashed for {symbol}: {e}")

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
        "books_done": 0, "leverage": leverage, "margin_ccy": margin_ccy
    }
    log.info(f"📝 Tracked: {side.upper()} {qty} {pair} @ {entry_price} | TP={tp_price} SL={sl_price}")

def clear_active_trade(pair, reason=""):
    old = active_trades.pop(pair, None)
    if old:
        cancel_native_sl(pair)
        log.info(f"🔓 Cleared: {pair} — {reason}")

def calc_quantity(coin_price, leverage):
    if FIXED_MARGIN_INR <= 0:
        return 0
    available_usdt = (FIXED_MARGIN_INR * WALLET_USAGE_PCT) / USDT_INR_RATE
    raw_qty = (available_usdt * leverage) / coin_price
    return round_down_quantity(raw_qty, coin_price)


# ═══════════════════════════════════════════════════════════
#  NATIVE SL — standalone stop-market order (no position ID needed)
#  Works with INR-margin positions
# ═══════════════════════════════════════════════════════════
native_sl_orders = {}  # {symbol: {"order_id": "...", "sl_price": ...}}

def place_native_sl(symbol, side, qty, sl_price, leverage, margin_ccy):
    """DISABLED — market_order + stop_price executes immediately on CoinDCX.
    Need to find correct stop order type before re-enabling."""
    log.info(f"🛡️ Native SL SKIPPED (disabled): {symbol} @ {sl_price} — needs stop order type fix")
    return


def cancel_native_sl(symbol):
    """Cancel existing native SL order."""
    if symbol not in native_sl_orders:
        return

    order_info = native_sl_orders.pop(symbol)
    order_id = order_info.get("order_id", "")

    if not order_id or order_id == "unknown":
        return

    try:
        body = {"id": order_id}
        resp = _sign_post("/exchange/v1/derivatives/futures/orders/cancel", body)
        if resp.status_code == 200:
            log.info(f"🛡️ Native SL cancelled: {symbol} | order={order_id}")
        else:
            log.warning(f"⚠️ Native SL cancel may have failed: {symbol} — likely already filled")
    except Exception as e:
        log.warning(f"⚠️ Native SL cancel error for {symbol}: {e}")


# ═══════════════════════════════════════════════════════════
#  WEBHOOK HANDLER — Pine drives all decisions
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
        book_pct = float(data.get("book_pct", 33)) / 100

        if action not in ("buy", "sell"):
            return jsonify({"status": "rejected", "reason": "invalid action"}), 200
        if not symbol:
            return jsonify({"status": "rejected", "reason": "missing symbol"}), 200

        # ─── ENTRY — initial position ─────────────────────────
        if alert_type == "entry":
            # Cooldown safety net — blocks redundant entry alerts within N seconds
            # of a prior entry/reverse on the same symbol (independent of active_trades).
            if symbol in last_action_time:
                elapsed = time.time() - last_action_time[symbol]
                if elapsed < ENTRY_COOLDOWN_SEC:
                    log.info(f"🚫 SKIP entry: {symbol} cooldown ({elapsed:.0f}s < {ENTRY_COOLDOWN_SEC}s)")
                    log_trade_event(symbol, action, "entry", "SKIP", f"cooldown {elapsed:.0f}s")
                    return jsonify({"status": "skipped", "reason": "cooldown"}), 200

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

            log.info(f"🚀 ENTRY {action.upper()} {quantity} {symbol} ({leverage}x) | TP={tp_price} SL={sl_price}")
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

            log_order_response(symbol, action, quantity, result, context="entry")

            order_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
            filled_qty = float(result.get("total_quantity", quantity)) if isinstance(result, dict) else quantity
            set_active_trade(symbol, action, filled_qty, coin_price, order_id,
                             tp_price=tp_price, sl_price=sl_price,
                             leverage=leverage, margin_ccy=margin_ccy)
            log_trade_event(symbol, action, "entry", "FILLED", f"TP={tp_price} SL={sl_price}")
            last_action_time[symbol] = time.time()

            # Place native SL on CoinDCX as safety net
            if sl_price:
                place_native_sl(symbol, action, filled_qty, sl_price, leverage, margin_ccy)

            return jsonify({"status": "success", "order": result}), 200

        # ─── BOOK — Pine says TP hit, execute partial close ───
        elif alert_type == "book":
            if symbol not in active_trades:
                log.info(f"⚠️ SKIP book: {symbol} not tracked")
                log_trade_event(symbol, action, "book", "SKIP", "not tracked")
                return jsonify({"status": "skipped"}), 200

            trade = active_trades[symbol]
            book_qty = round_down_quantity(trade["original_qty"] * book_pct, coin_price)
            book_qty = min(book_qty, trade["qty"])

            if book_qty <= 0:
                log.info(f"⚠️ SKIP book: {symbol} — book qty too small")
                return jsonify({"status": "skipped", "reason": "book qty too small"}), 200

            # Cancel old native SL (qty is changing)
            cancel_native_sl(symbol)

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
                # Re-place old SL since book failed
                if trade.get("sl_price"):
                    place_native_sl(symbol, trade["side"], trade["qty"], trade["sl_price"], leverage, margin_ccy)
                return jsonify({"status": "rejected", "reason": err}), 200

            log_order_response(symbol, close_side, book_qty, result, context="book")

            # Update tracked qty and TP/SL from Pine
            trade["qty"] -= book_qty
            trade["books_done"] += 1
            if tp_price:
                trade["tp_price"] = tp_price
            if sl_price:
                trade["sl_price"] = sl_price

            log.info(f"✅ Booked {book_qty} {symbol} — remaining: {trade['qty']:.4f} | new TP={tp_price} SL={sl_price}")
            log_trade_event(symbol, action, "book", "FILLED", f"book #{trade['books_done']}, remaining={trade['qty']:.4f}")

            # Re-place native SL with updated qty and new SL level
            if trade["qty"] > 0 and sl_price:
                place_native_sl(symbol, trade["side"], trade["qty"], sl_price, leverage, margin_ccy)

            return jsonify({"status": "booked", "remaining_qty": trade["qty"]}), 200

        # ─── REVERSE — SL hit, close remaining + open opposite ──
        elif alert_type == "reverse":
            cancel_native_sl(symbol)

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
                        log.warning(f"⚠️ Reverse close failed (likely already closed): {err}")
                    else:
                        log_order_response(symbol, close_side, close_qty, close_result, context="reverse-close")

                clear_active_trade(symbol, "reverse — SL hit")
                log_trade_event(symbol, close_side, "reverse_close", "FILLED", "Pine reverse")

            time.sleep(1)

            quantity = calc_quantity(coin_price, leverage)
            if quantity <= 0:
                log.error(f"❌ REJECT reverse entry: {symbol} — qty=0")
                return jsonify({"status": "rejected", "reason": "reverse entry qty=0"}), 200

            log.info(f"🔄 REVERSE entry: {action.upper()} {quantity} {symbol} | TP={tp_price} SL={sl_price}")
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

            log_order_response(symbol, action, quantity, result, context="reverse-open")

            order_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
            filled_qty = float(result.get("total_quantity", quantity)) if isinstance(result, dict) else quantity
            set_active_trade(symbol, action, filled_qty, coin_price, order_id,
                             tp_price=tp_price, sl_price=sl_price,
                             leverage=leverage, margin_ccy=margin_ccy)
            log_trade_event(symbol, action, "reverse_entry", "FILLED", f"TP={tp_price} SL={sl_price}")
            last_action_time[symbol] = time.time()

            if sl_price:
                place_native_sl(symbol, action, filled_qty, sl_price, leverage, margin_ccy)

            return jsonify({"status": "reversed", "order": result}), 200

        # ─── CLOSE — kill switch ──────────────────────────────
        elif alert_type == "close":
            reason = data.get("reason", "unknown")
            ret_pct = data.get("return_pct", "?")
            log.info(f"☠️ KILL SWITCH: {symbol} — reason={reason}, return={ret_pct}%")

            cancel_native_sl(symbol)

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
                    else:
                        log_order_response(symbol, close_side, close_qty, result, context="kill-close")

                clear_active_trade(symbol, f"kill switch — return {ret_pct}%")
                log_trade_event(symbol, close_side, "kill", "FILLED", f"return={ret_pct}%")
            else:
                log.info(f"⚠️ Kill for {symbol} but not tracked — no action needed")
                log_trade_event(symbol, action, "kill", "SKIP", "not tracked")

            return jsonify({"status": "killed", "symbol": symbol}), 200

        else:
            return jsonify({"status": "unknown_type", "type": alert_type}), 200

    except Exception as e:
        log.error(f"❌ Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 200


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
        "time": datetime.now().isoformat()
    })

@app.route("/clear-lock", methods=["POST", "GET"])
def clear_lock():
    symbol = request.args.get("symbol")
    if symbol:
        clear_active_trade(symbol, "manual clear")
        return jsonify({"status": "ok", "cleared": symbol})
    else:
        for sym in list(native_sl_orders.keys()):
            cancel_native_sl(sym)
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

# ═══════════════════════════════════════════════════════════
#  PERIODIC POSITION SYNC — passive reconciliation
#  Pulls CoinDCX positions every N seconds, compares to active_trades,
#  LOGS mismatches only. Does NOT auto-close or auto-open anything.
#  Known limitation: CoinDCX positions API returns active_pos=0 for
#  INR-margin positions, so sync is mostly useful for USDT-margin.
#  Even so, it surfaces drift when positions do appear.
# ═══════════════════════════════════════════════════════════
SYNC_ENABLED      = os.environ.get("SYNC_POSITIONS", "true").lower() == "true"
SYNC_INTERVAL_SEC = int(os.environ.get("SYNC_INTERVAL_SEC", "60"))
SYNC_MISMATCH_TOL = float(os.environ.get("SYNC_MISMATCH_TOL", "0.10"))  # 10% tolerance

def sync_positions_worker():
    log.info(f"🔄 sync_positions thread started (interval={SYNC_INTERVAL_SEC}s, tol={SYNC_MISMATCH_TOL*100:.0f}%)")
    while True:
        try:
            time.sleep(SYNC_INTERVAL_SEC)
            if not active_trades:
                continue

            positions = client.get_positions()
            if not isinstance(positions, list):
                log.warning(f"⚠️ SYNC: unexpected positions response type={type(positions).__name__}")
                continue

            # Build {pair: position_dict} from CoinDCX response
            cdcx_by_pair = {}
            for p in positions:
                if isinstance(p, dict) and p.get("pair"):
                    cdcx_by_pair[p["pair"]] = p

            # Iterate a snapshot to avoid mutation-during-iteration races
            snapshot = dict(active_trades)
            for symbol, tracked in snapshot.items():
                cdcx = cdcx_by_pair.get(symbol)
                tracked_qty  = float(tracked.get("qty", 0))
                tracked_side = tracked.get("side", "?")

                if cdcx is None:
                    log.warning(f"⚠️ SYNC: {symbol} tracked ({tracked_side} {tracked_qty:.4f}) "
                                f"but NOT in CoinDCX positions response")
                    continue

                try:
                    active_pos = float(cdcx.get("active_pos", 0))
                except (TypeError, ValueError):
                    active_pos = 0.0

                cdcx_qty  = abs(active_pos)
                cdcx_side = "buy" if active_pos > 0 else ("sell" if active_pos < 0 else "flat")

                # INR-margin typically returns active_pos=0 — note it but don't spam
                if cdcx_qty < 1e-6:
                    log.info(f"🔄 SYNC: {symbol} tracked={tracked_side} {tracked_qty:.4f}, "
                             f"CoinDCX active_pos=0 (normal for INR-margin)")
                    continue

                # Side mismatch
                if cdcx_side != tracked_side:
                    log.warning(f"⚠️ SYNC SIDE MISMATCH: {symbol} tracked={tracked_side} "
                                f"but CoinDCX side={cdcx_side} qty={cdcx_qty:.4f}")
                    continue

                # Qty mismatch beyond tolerance
                if tracked_qty > 0:
                    diff_ratio = abs(cdcx_qty - tracked_qty) / tracked_qty
                    if diff_ratio > SYNC_MISMATCH_TOL:
                        pct = cdcx_qty / tracked_qty * 100 if tracked_qty > 0 else 0
                        log.warning(f"⚠️ SYNC QTY MISMATCH: {symbol} tracked={tracked_qty:.4f}, "
                                    f"CoinDCX={cdcx_qty:.4f} ({pct:.1f}%)")
        except Exception as e:
            log.error(f"❌ sync_positions loop error: {e}")

def start_sync_thread():
    if not SYNC_ENABLED:
        log.info("🔄 sync_positions DISABLED (set SYNC_POSITIONS=true to enable)")
        return
    t = threading.Thread(target=sync_positions_worker, daemon=True, name="sync-positions")
    t.start()


# ─── Startup ──────────────────────────────────────────────────
load_tick_sizes()
start_sync_thread()
log.info("🤖 Trail TP/SL Rev Bot ready — Pine-driven bookings + native SL + fill-quality logging + sync")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
