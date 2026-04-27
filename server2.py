import os
import json
import math
import time
import logging
import threading
import requests
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

# ═══════════════════════════════════════════════════════════
#  AUTO PROFIT-LOCK — close all positions when net ROE ≥ threshold
# ═══════════════════════════════════════════════════════════
# When (sum of unrealized P&L across all active positions) / (sum of
# deployed margins) ≥ PROFIT_LOCK_PCT, the monitor thread:
#   1. closes every position in active_trades via market order
#   2. clears active_trades
#   3. activates a cooldown window that rejects every webhook except fresh
#      'entry' alerts, for COOLDOWN_AFTER_LOCK_SEC seconds
#
# Math: net% = sum((mark - entry) * qty * dir) / sum(margin) where
# margin per position is stored on set_active_trade (entry_price * qty / leverage).
# Polls CoinDCX public ticker endpoint (no auth) every POLL_INTERVAL_SEC.
PROFIT_LOCK_ENABLED        = os.environ.get("PROFIT_LOCK_ENABLED", "true").lower() == "true"
PROFIT_LOCK_PCT            = float(os.environ.get("PROFIT_LOCK_PCT", "13.0"))
POLL_INTERVAL_SEC          = int(os.environ.get("PROFIT_LOCK_POLL_SEC", "10"))
COOLDOWN_AFTER_LOCK_SEC    = int(os.environ.get("PROFIT_LOCK_COOLDOWN_SEC", "300"))
TICKER_CACHE_TTL_SEC       = 5  # avoid hammering ticker endpoint

# Shared state (thread-safe via simple GIL semantics — all reads/writes atomic)
_profit_lock_until = 0.0   # epoch timestamp; webhooks restricted until this
_ticker_cache = {}          # {symbol: (price, fetched_at)}

# ═══════════════════════════════════════════════════════════
#  DAILY PROFIT CAP — hard stop after N% cumulative locked
# ═══════════════════════════════════════════════════════════
# Tracks cumulative profit-lock %s captured across the day. When the sum
# reaches DAILY_CAP_PCT (default 26%), the server enters a hard-pause state:
# every webhook (entry, book, reverse, kill) is rejected until either:
#   (a) midnight IST passes — counter auto-resets, pause lifts
#   (b) operator hits POST /daily-cap/reset?secret=... to manually unpause
#
# Each profit-lock event contributes its triggering net% to the running sum.
# Example: lock #1 fires at 13.5%, lock #2 fires at 14.2% → cumulative 27.7%
# → daily cap exceeded → pause until midnight IST.
DAILY_CAP_ENABLED          = os.environ.get("DAILY_CAP_ENABLED", "true").lower() == "true"
DAILY_CAP_PCT              = float(os.environ.get("DAILY_CAP_PCT", "26.0"))

# Shared state — reset at midnight IST
_daily_locked_pct_sum      = 0.0   # cumulative net% from all locks today
_daily_lock_count          = 0     # number of locks fired today
_daily_paused              = False # True once cap is hit (until midnight or manual reset)
_daily_counter_date        = None  # tracks which IST date the counter belongs to

# IST timezone constant for date comparisons
IST_TZ = timezone(timedelta(hours=5, minutes=30))

FIXED_MARGIN_INR = float(os.environ.get("FIXED_MARGIN_INR", "0"))
USDT_INR_RATE = float(os.environ.get("USDT_INR_RATE", "98"))
WALLET_USAGE_PCT = float(os.environ.get("WALLET_USAGE_PCT", "100")) / 100

# ─── Event Log ───
trade_log = []
MAX_LOG = 50

# ═══════════════════════════════════════════════════════════
#  PROFIT-LOCK MONITOR
# ═══════════════════════════════════════════════════════════
def fetch_mark_price(symbol):
    """Get current mark price for a futures symbol.

    Strategy (in order):
      1. CoinDCX public futures endpoint — real-time mark price (mp field).
         This matches exactly what CoinDCX's UI uses for ROE calculation.
         Cached for TICKER_CACHE_TTL_SEC to batch calls when many symbols
         are queried in the same polling cycle.
      2. Fall back to last_known_price recorded from the most recent webhook
         (Pine sends `close` on every alert — stale between bar closes but
         always available).
      3. Fall back to entry_price so compute_net_roe returns 0% for this
         symbol rather than crashing.
    """
    now = time.time()

    # Primary: cached real-time mark price
    cached = _ticker_cache.get(symbol)
    if cached and (now - cached[1]) < TICKER_CACHE_TTL_SEC:
        return cached[0]

    # Primary: live fetch from public futures endpoint
    try:
        resp = _req.get(
            "https://public.coindcx.com/market_data/v3/current_prices/futures/rt",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            # Response shape: {"ts":..., "prices": {"B-LIT_USDT": {"mp": ..., "ls": ..., ...}}}
            prices = data.get("prices") if isinstance(data, dict) else None
            if isinstance(prices, dict):
                # Populate cache for all symbols in one shot — saves repeated fetches
                # when compute_net_roe iterates across multiple active trades
                for sym, row in prices.items():
                    if not isinstance(row, dict):
                        continue
                    px_raw = row.get("mp") or row.get("ls") or row.get("last_price")
                    if px_raw is None:
                        continue
                    try:
                        _ticker_cache[sym] = (float(px_raw), now)
                    except (TypeError, ValueError):
                        continue
                # Return this symbol's fresh price if present
                if symbol in _ticker_cache:
                    return _ticker_cache[symbol][0]
    except Exception as e:
        log.debug(f"ticker fetch failed for {symbol}: {e}")

    # Fallback: webhook-recorded price on the trade itself
    trade = active_trades.get(symbol)
    if trade:
        lkp = trade.get("last_known_price")
        lkp_time = trade.get("last_known_price_time", 0)
        if lkp and (time.time() - lkp_time) < 600:
            return float(lkp)
        # Last resort: entry price (returns 0% pnl for this symbol)
        return float(trade.get("entry_price", 0) or 0) or None
    return None


def record_webhook_price(symbol, price):
    """Called from webhook handler: stores the current price on the active trade
    as a fallback for when the public endpoint is unavailable."""
    if symbol in active_trades and price:
        try:
            active_trades[symbol]["last_known_price"] = float(price)
            active_trades[symbol]["last_known_price_time"] = time.time()
        except (TypeError, ValueError):
            pass


def compute_net_roe():
    """Return (net_pnl_inr, total_margin_inr, net_pct) across all active trades.
    Returns (None, None, None) if any price is unavailable."""
    if not active_trades:
        return 0.0, 0.0, 0.0
    total_pnl = 0.0
    total_margin = 0.0
    for sym, trade in list(active_trades.items()):
        mark = fetch_mark_price(sym)
        if mark is None or mark <= 0:
            return None, None, None
        entry = float(trade.get("entry_price", 0) or 0)
        qty   = float(trade.get("qty", 0) or 0)
        side  = trade.get("side", "")
        lev   = int(trade.get("leverage", DEFAULT_LEVERAGE) or DEFAULT_LEVERAGE)
        if entry <= 0 or qty <= 0:
            continue
        direction = 1 if side == "buy" else -1
        pnl_usd = (mark - entry) * qty * direction
        pnl_inr = pnl_usd * float(os.environ.get("USDT_INR_RATE", "98"))
        margin_inr = (entry * qty / lev) * float(os.environ.get("USDT_INR_RATE", "98"))
        total_pnl += pnl_inr
        total_margin += margin_inr
    if total_margin <= 0:
        return total_pnl, 0.0, 0.0
    return total_pnl, total_margin, (total_pnl / total_margin) * 100


# ─── DAILY CAP HELPERS ─────────────────────────────────────
def _current_ist_date():
    """Return today's date in IST as a date object."""
    return datetime.now(IST_TZ).date()


def _check_and_reset_daily_counter():
    """If we've crossed midnight IST since the counter was last set, reset.
    Called at the top of every webhook + every monitor cycle."""
    global _daily_locked_pct_sum, _daily_lock_count, _daily_paused, _daily_counter_date
    today = _current_ist_date()
    if _daily_counter_date != today:
        if _daily_counter_date is not None:
            log.info(f"🌅 IST midnight crossed — resetting daily counter "
                     f"(previous day: locks={_daily_lock_count}, "
                     f"sum={_daily_locked_pct_sum:.2f}%, paused={_daily_paused})")
        _daily_locked_pct_sum = 0.0
        _daily_lock_count = 0
        _daily_paused = False
        _daily_counter_date = today


def _bump_daily_counter(lock_pct):
    """Called inside close_all_positions when a lock fires.
    Adds the triggering net% to the cumulative sum and trips the daily pause
    if the cap is exceeded."""
    global _daily_locked_pct_sum, _daily_lock_count, _daily_paused
    _check_and_reset_daily_counter()
    _daily_locked_pct_sum += lock_pct
    _daily_lock_count += 1
    log.info(f"📊 Daily progress: lock #{_daily_lock_count} added {lock_pct:.2f}% "
             f"→ cumulative {_daily_locked_pct_sum:.2f}% / cap {DAILY_CAP_PCT}%")
    if DAILY_CAP_ENABLED and _daily_locked_pct_sum >= DAILY_CAP_PCT and not _daily_paused:
        _daily_paused = True
        log.warning(f"🛑 DAILY CAP REACHED — cumulative {_daily_locked_pct_sum:.2f}% "
                    f"≥ {DAILY_CAP_PCT}% — ALL webhooks paused until IST midnight "
                    f"or manual /daily-cap/reset")


def daily_cap_active():
    """Returns True if daily cap is hit and webhooks should be rejected."""
    _check_and_reset_daily_counter()
    return DAILY_CAP_ENABLED and _daily_paused


def close_all_positions(trigger_reason="profit lock", trigger_pct=None):
    """Close every position in active_trades via market order, clear state,
    cancel any native SLs, and activate the post-lock cooldown.
    If trigger_pct is provided, also bumps the daily cumulative counter."""
    global _profit_lock_until
    snapshot = list(active_trades.items())
    log.info(f"🔒 PROFIT LOCK triggered ({trigger_reason}) — closing {len(snapshot)} positions")
    for sym, trade in snapshot:
        try:
            close_qty = float(trade.get("qty", 0) or 0)
            if close_qty <= 0:
                continue
            close_side = "sell" if trade.get("side") == "buy" else "buy"
            lev = trade.get("leverage", DEFAULT_LEVERAGE)
            mcy = trade.get("margin_ccy", DEFAULT_MARGIN)
            log.info(f"🔻 LOCK close: {close_side.upper()} {close_qty} {sym}")
            result = client.place_order(
                pair=sym, side=close_side, order_type="market_order",
                total_quantity=close_qty, leverage=lev, margin_currency=mcy
            )
            if isinstance(result, dict) and result.get("status") == "error":
                log.warning(f"⚠️ Lock close failed for {sym}: {result.get('message','')}")
            log_trade_event(sym, close_side, "profit_lock", "FILLED", trigger_reason)
        except Exception as e:
            log.error(f"❌ Lock close exception for {sym}: {e}", exc_info=True)
    # Cancel native SLs, clear tracking
    for sym in list(native_sl_orders.keys()):
        try:
            cancel_native_sl(sym)
        except Exception:
            pass
    active_trades.clear()
    _profit_lock_until = time.time() + COOLDOWN_AFTER_LOCK_SEC
    cooldown_end = datetime.fromtimestamp(_profit_lock_until).strftime("%H:%M:%S")
    log.info(f"✅ PROFIT LOCK complete — all positions closed, cooldown until {cooldown_end}")
    # Bump daily counter (may trip the daily cap)
    if trigger_pct is not None:
        _bump_daily_counter(trigger_pct)


def profit_lock_worker():
    """Background thread: polls net ROE every POLL_INTERVAL_SEC; triggers close-all
    when threshold crossed. Skips polling during cooldown window."""
    log.info(f"🎯 Profit-lock monitor started — threshold={PROFIT_LOCK_PCT}%, "
             f"poll={POLL_INTERVAL_SEC}s, cooldown={COOLDOWN_AFTER_LOCK_SEC}s")
    while True:
        try:
            time.sleep(POLL_INTERVAL_SEC)
            if not PROFIT_LOCK_ENABLED:
                continue
            if time.time() < _profit_lock_until:
                continue  # in cooldown window
            if not active_trades:
                continue
            pnl, margin, pct = compute_net_roe()
            if pct is None:
                log.debug("profit-lock: price fetch unavailable this cycle")
                continue
            # Verbose log every cycle so you can see it working in Railway
            log.info(f"🎯 net_roe check: pnl=₹{pnl:.2f} margin=₹{margin:.2f} "
                     f"net={pct:.2f}% (threshold={PROFIT_LOCK_PCT}%) "
                     f"positions={len(active_trades)}")
            if pct >= PROFIT_LOCK_PCT:
                close_all_positions(
                    trigger_reason=f"net ROE {pct:.2f}% ≥ {PROFIT_LOCK_PCT}%",
                    trigger_pct=pct
                )
        except Exception as e:
            log.error(f"profit-lock worker error: {e}", exc_info=True)


def in_profit_lock_cooldown():
    """Return True if we're currently in the post-lock cooldown window."""
    return time.time() < _profit_lock_until


def cooldown_remaining_sec():
    remaining = _profit_lock_until - time.time()
    return max(0, int(remaining))


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

        # Record the freshest price for this symbol — used by profit-lock monitor
        if symbol and coin_price > 0:
            record_webhook_price(symbol, coin_price)

        if action not in ("buy", "sell"):
            return jsonify({"status": "rejected", "reason": "invalid action"}), 200
        if not symbol:
            return jsonify({"status": "rejected", "reason": "missing symbol"}), 200

        # ─── DAILY CAP HARD-STOP GATE ─────────────────────────
        # If today's cumulative locked profit ≥ DAILY_CAP_PCT, reject EVERY
        # webhook (entry/book/reverse/kill) regardless of type. State persists
        # until IST midnight rollover OR manual /daily-cap/reset.
        if daily_cap_active():
            log.info(f"🛑 DAILY CAP: rejecting {alert_type} for {symbol} "
                     f"(cumulative {_daily_locked_pct_sum:.2f}% / cap {DAILY_CAP_PCT}%)")
            log_trade_event(symbol, action, alert_type, "DAILY_CAP",
                            f"sum={_daily_locked_pct_sum:.2f}%")
            return jsonify({
                "status": "rejected",
                "reason": f"daily cap reached ({_daily_locked_pct_sum:.2f}% ≥ {DAILY_CAP_PCT}%)"
            }), 200

        # ─── PROFIT-LOCK COOLDOWN GATE ────────────────────────
        # After an auto-close-all, reject ALL webhooks (including entries)
        # for COOLDOWN_AFTER_LOCK_SEC seconds. This lets Pine's internal
        # state drift back into sync before we accept new trades.
        if in_profit_lock_cooldown():
            remaining = cooldown_remaining_sec()
            log.info(f"🔒 COOLDOWN: rejecting {alert_type} for {symbol} — {remaining}s left")
            log_trade_event(symbol, action, alert_type, "COOLDOWN", f"{remaining}s remaining")
            return jsonify({"status": "rejected", "reason": f"profit-lock cooldown ({remaining}s)"}), 200

        # ─── ENTRY — initial position ─────────────────────────
        if alert_type == "entry":
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

            order_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
            filled_qty = float(result.get("total_quantity", quantity)) if isinstance(result, dict) else quantity
            set_active_trade(symbol, action, filled_qty, coin_price, order_id,
                             tp_price=tp_price, sl_price=sl_price,
                             leverage=leverage, margin_ccy=margin_ccy)
            log_trade_event(symbol, action, "entry", "FILLED", f"TP={tp_price} SL={sl_price}")

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

            order_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
            filled_qty = float(result.get("total_quantity", quantity)) if isinstance(result, dict) else quantity
            set_active_trade(symbol, action, filled_qty, coin_price, order_id,
                             tp_price=tp_price, sl_price=sl_price,
                             leverage=leverage, margin_ccy=margin_ccy)
            log_trade_event(symbol, action, "reverse_entry", "FILLED", f"TP={tp_price} SL={sl_price}")

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
    pnl, margin, pct = compute_net_roe()
    pct_str = f"{pct:.2f}" if pct is not None else "unavailable"
    _check_and_reset_daily_counter()  # ensure counter reflects current IST date
    return jsonify({
        "active_trades": active_trades,
        "native_sl_orders": native_sl_orders,
        "positions": len(active_trades),
        "profit_lock": {
            "enabled": PROFIT_LOCK_ENABLED,
            "threshold_pct": PROFIT_LOCK_PCT,
            "current_net_pct": pct_str,
            "net_pnl_inr": round(pnl, 2) if pnl is not None else None,
            "total_margin_inr": round(margin, 2) if margin is not None else None,
            "in_cooldown": in_profit_lock_cooldown(),
            "cooldown_remaining_sec": cooldown_remaining_sec(),
        },
        "daily_cap": {
            "enabled": DAILY_CAP_ENABLED,
            "cap_pct": DAILY_CAP_PCT,
            "cumulative_locked_pct": round(_daily_locked_pct_sum, 2),
            "lock_count_today": _daily_lock_count,
            "is_paused": _daily_paused,
            "ist_date": str(_daily_counter_date) if _daily_counter_date else None,
            "ist_now": datetime.now(IST_TZ).isoformat(),
        },
        "time": datetime.now().isoformat()
    })

@app.route("/profit-lock/check", methods=["GET"])
def profit_lock_check():
    """Force a manual profit-lock check; does NOT trigger close unless threshold met."""
    pnl, margin, pct = compute_net_roe()
    # Per-symbol price diagnostics
    price_sources = {}
    now = time.time()
    for sym, trade in active_trades.items():
        lkp = trade.get("last_known_price")
        lkp_time = trade.get("last_known_price_time", 0)
        age = int(now - lkp_time) if lkp_time else None
        cached = _ticker_cache.get(sym)
        price_sources[sym] = {
            "webhook_price": lkp,
            "webhook_price_age_sec": age,
            "public_endpoint_cache": cached[0] if cached else None,
            "using": (
                "webhook" if (lkp and age is not None and age < 600)
                else ("public_endpoint" if cached else "none/fallback_to_entry")
            ),
        }
    return jsonify({
        "net_pnl_inr": pnl,
        "total_margin_inr": margin,
        "net_pct": pct,
        "threshold_pct": PROFIT_LOCK_PCT,
        "would_trigger": (pct is not None and pct >= PROFIT_LOCK_PCT),
        "in_cooldown": in_profit_lock_cooldown(),
        "cooldown_remaining_sec": cooldown_remaining_sec(),
        "price_sources": price_sources,
    })

@app.route("/profit-lock/force", methods=["POST", "GET"])
def profit_lock_force():
    """Manually trigger close-all-and-lock (for emergencies / testing).
    Requires ?secret=... matching WEBHOOK_SECRET.
    Computes current net% and contributes it to the daily cap counter."""
    if WEBHOOK_SECRET and request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    if not active_trades:
        return jsonify({"status": "no_positions", "active": 0})
    count = len(active_trades)
    pnl, margin, pct = compute_net_roe()
    # If net% can't be computed, default to 0 — manual force shouldn't trip cap
    close_all_positions(
        trigger_reason="manual force",
        trigger_pct=(pct if pct is not None else 0.0)
    )
    return jsonify({"status": "locked", "closed": count,
                    "trigger_pct": pct,
                    "cooldown_remaining_sec": cooldown_remaining_sec(),
                    "daily_cumulative_pct": _daily_locked_pct_sum,
                    "daily_paused": _daily_paused})

@app.route("/daily-cap/reset", methods=["POST", "GET"])
def daily_cap_reset():
    """Manually unpause the daily cap and reset the cumulative counter to 0.
    Useful when you want to start a new cycle without waiting for IST midnight.
    Requires ?secret=... matching WEBHOOK_SECRET.
    Does NOT clear active_trades or affect the profit-lock cooldown."""
    global _daily_locked_pct_sum, _daily_lock_count, _daily_paused, _daily_counter_date
    if WEBHOOK_SECRET and request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    prev_sum = _daily_locked_pct_sum
    prev_count = _daily_lock_count
    prev_paused = _daily_paused
    _daily_locked_pct_sum = 0.0
    _daily_lock_count = 0
    _daily_paused = False
    _daily_counter_date = _current_ist_date()
    log.info(f"🔄 Daily cap manually reset by operator "
             f"(was: locks={prev_count}, sum={prev_sum:.2f}%, paused={prev_paused})")
    return jsonify({
        "status": "reset",
        "previous": {
            "lock_count": prev_count,
            "cumulative_pct": round(prev_sum, 2),
            "was_paused": prev_paused,
        },
        "current": {
            "lock_count": 0,
            "cumulative_pct": 0.0,
            "is_paused": False,
            "ist_date": str(_daily_counter_date),
        }
    })

@app.route("/daily-cap/check", methods=["GET"])
def daily_cap_check():
    """Read-only view of daily cap state."""
    _check_and_reset_daily_counter()
    return jsonify({
        "enabled": DAILY_CAP_ENABLED,
        "cap_pct": DAILY_CAP_PCT,
        "cumulative_locked_pct": round(_daily_locked_pct_sum, 2),
        "remaining_pct": round(max(0, DAILY_CAP_PCT - _daily_locked_pct_sum), 2),
        "lock_count_today": _daily_lock_count,
        "is_paused": _daily_paused,
        "ist_date": str(_daily_counter_date) if _daily_counter_date else None,
        "ist_now": datetime.now(IST_TZ).isoformat(),
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

# ─── Startup ──────────────────────────────────────────────────
load_tick_sizes()
log.info("🤖 Trail TP/SL Rev Bot ready — Pine-driven bookings + native SL")

# Initialize daily-cap counter for today's IST date
_check_and_reset_daily_counter()
log.info(f"📅 Daily cap initialized — IST date {_daily_counter_date}, "
         f"cap={DAILY_CAP_PCT}%, enabled={DAILY_CAP_ENABLED}")

# Start the profit-lock monitor thread (unconditional — it self-gates on the
# PROFIT_LOCK_ENABLED env var so you can flip it without a redeploy).
_profit_lock_thread = threading.Thread(target=profit_lock_worker, daemon=True)
_profit_lock_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
