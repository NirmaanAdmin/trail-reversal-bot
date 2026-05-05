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

def _signed_get(endpoint, body=None):
    """Some CoinDCX endpoints (futures wallets) take signed GETs.
    Sends the signed JSON body alongside a GET. Returns the raw response."""
    body = (body or {}).copy()
    body["timestamp"] = int(round(time.time() * 1000))
    json_body = json.dumps(body, separators=(",", ":"))
    sig = _hmac.new(API_SECRET.encode(), json_body.encode(), _hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-AUTH-APIKEY": API_KEY, "X-AUTH-SIGNATURE": sig}
    return _req.get(f"https://api.coindcx.com{endpoint}", data=json_body, headers=headers, timeout=15)

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

# ─── Persist active_trades across container restarts ──────────
# Without this, a Railway restart wipes the in-memory dict and orphans
# any open positions on CoinDCX (profit-lock can't see them, books fail
# to match). Saves on every set/clear/book/close-all. Loads on startup.
ACTIVE_TRADES_FILE = os.environ.get("ACTIVE_TRADES_FILE", "/app/data/active_trades.json")

def _save_active_trades():
    try:
        os.makedirs(os.path.dirname(ACTIVE_TRADES_FILE), exist_ok=True)
        with open(ACTIVE_TRADES_FILE, "w") as f:
            json.dump(active_trades, f)
    except Exception as e:
        log.warning(f"⚠️ Failed to persist active_trades: {e}")

def _load_active_trades():
    try:
        with open(ACTIVE_TRADES_FILE) as f:
            data = json.load(f)
        active_trades.update(data)
        if data:
            log.info(f"📂 Restored {len(data)} active trade(s) from disk: {list(data.keys())}")
        else:
            log.info("📂 active_trades file empty — starting fresh")
    except FileNotFoundError:
        log.info(f"📂 No active_trades file at {ACTIVE_TRADES_FILE} — starting fresh")
    except Exception as e:
        log.warning(f"⚠️ Failed to load active_trades ({e}) — starting fresh")

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

# ═══════════════════════════════════════════════════════════
#  BASELINE EQUITY TARGET — auto-rollover profit lock
# ═══════════════════════════════════════════════════════════
# Tracks cumulative realized P&L since a baseline was set. When current
# equity (baseline + realized + unrealized) crosses baseline × (1 + trigger%),
# closes all positions, banks the gain, and rolls baseline forward by
# rollover% (less than trigger% — the gap is "banked profit").
#
# Example with trigger=6%, rollover=5%:
#   baseline = 2000 → trigger at 2120 → new baseline = 2100 (banked 20)
#   baseline = 2100 → trigger at 2226 → new baseline = 2205 (banked 21)
#
# State persists across restarts in BASELINE_FILE (use Railway volume for
# durability; without volume, file resets on every redeploy and you re-set
# baseline manually via /baseline/set).
BASELINE_ENABLED        = os.environ.get("BASELINE_ENABLED", "false").lower() == "true"
BASELINE_TRIGGER_PCT    = float(os.environ.get("BASELINE_TRIGGER_PCT", "6.0"))
BASELINE_ROLLOVER_PCT   = float(os.environ.get("BASELINE_ROLLOVER_PCT", "5.0"))
BASELINE_COOLDOWN_SEC   = int(os.environ.get("BASELINE_COOLDOWN_SEC", "60"))
BASELINE_FILE           = os.environ.get("BASELINE_FILE", "/app/baseline_state.json")

# Shared state
_baseline_inr           = None   # current baseline (INR), None = not set
_baseline_realized_pnl  = 0.0    # realized P&L in INR since baseline was last set/rolled
_baseline_lock_count    = 0      # number of times baseline has triggered (since reset)
_baseline_last_lock_at  = None   # ISO timestamp of last trigger
_baseline_history       = []     # list of {timestamp, old_baseline, trigger_equity, new_baseline, realized}
_baseline_cooldown_until = 0.0   # epoch — webhooks rejected (except book/close) until this

FIXED_MARGIN_INR = float(os.environ.get("FIXED_MARGIN_INR", "0"))
USDT_INR_RATE = float(os.environ.get("USDT_INR_RATE", "98"))
WALLET_USAGE_PCT = float(os.environ.get("WALLET_USAGE_PCT", "100")) / 100

# ─── SL LOCKOUT ──────────────────────────────────────────────
# When Pine emits an sl_wait close on a symbol, mark it as locked.
# Any subsequent entry/reverse webhook for that symbol is rejected
# until the lockout expires. Pine signals continue arriving and are
# logged but no orders are placed on CoinDCX. After SL_LOCKOUT_SEC
# elapses, the symbol is freed and the next Pine signal is accepted
# normally. State is in-memory only — Railway restart wipes it (the
# worst case is one extra trade we'd otherwise have skipped).
SL_LOCKOUT_ENABLED   = os.environ.get("SL_LOCKOUT_ENABLED", "true").lower() == "true"
SL_LOCKOUT_SEC       = int(os.environ.get("SL_LOCKOUT_SEC", "600"))    # 10 min
SL_LOCKOUT_CHECK_SEC = int(os.environ.get("SL_LOCKOUT_CHECK_SEC", "30"))
_sl_lockout = {}  # {symbol: unlock_epoch_time}

def mark_sl_lockout(symbol, reason=""):
    """Called from the sl_wait close branch — locks the symbol for
    SL_LOCKOUT_SEC seconds. Subsequent calls reset the timer."""
    if not SL_LOCKOUT_ENABLED:
        return
    until = time.time() + SL_LOCKOUT_SEC
    _sl_lockout[symbol] = until
    mins = SL_LOCKOUT_SEC // 60
    log.info(f"🚫 SL LOCKOUT armed: {symbol} blocked for {mins}m ({reason})")

def in_sl_lockout(symbol):
    """Returns (locked: bool, remaining_sec: int). Lazy-deletes expired entries."""
    if not SL_LOCKOUT_ENABLED:
        return False, 0
    until = _sl_lockout.get(symbol, 0)
    if until == 0:
        return False, 0
    remaining = int(until - time.time())
    if remaining <= 0:
        _sl_lockout.pop(symbol, None)
        return False, 0
    return True, remaining

def sl_lockout_worker():
    """Background thread: every SL_LOCKOUT_CHECK_SEC, prunes expired
    locks and emits a heartbeat log of what's currently locked."""
    log.info(f"🚫 SL lockout monitor started — enabled={SL_LOCKOUT_ENABLED}, "
             f"duration={SL_LOCKOUT_SEC}s, check_every={SL_LOCKOUT_CHECK_SEC}s")
    while True:
        try:
            time.sleep(SL_LOCKOUT_CHECK_SEC)
            if not SL_LOCKOUT_ENABLED:
                continue
            now = time.time()
            expired = [s for s, until in list(_sl_lockout.items()) if until <= now]
            for s in expired:
                _sl_lockout.pop(s, None)
                log.info(f"✅ SL LOCKOUT cleared: {s} — symbol free to re-enter")
            if _sl_lockout:
                pretty = ", ".join(
                    f"{s}({int(u-now)}s left)" for s, u in _sl_lockout.items()
                )
                log.info(f"🚫 SL lockout active: {pretty}")
        except Exception as e:
            log.error(f"sl_lockout_worker error: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════
#  TARGET CURRENT VALUE — wallet-based hard stop
#
#  Polls CoinDCX wallet endpoint every TARGET_POLL_SEC. Computes the
#  real wallet balance (Available + Locked margin) — NO unrealized P&L.
#  This means the trigger fires when actually-banked money crosses target,
#  not when paper P&L crosses target. More conservative and more predictable
#  than including unrealized (which lags due to mark price API drift).
#
#  When wallet_balance >= TARGET_CURRENT_VALUE, a flag flips and entry/reverse
#  webhooks are rejected. Books and closes still pass through so existing
#  positions can resolve. To resume trading, set a new (higher) target via
#  /target/set?value=N&secret=... — this clears the hit flag.
#
#  State persists across Railway restarts in TARGET_FILE on the volume.
# ═══════════════════════════════════════════════════════════
TARGET_ENABLED        = os.environ.get("TARGET_ENABLED", "true").lower() == "true"
TARGET_CURRENT_VALUE  = float(os.environ.get("TARGET_CURRENT_VALUE", "0"))  # 0 = disabled
TARGET_POLL_SEC       = int(os.environ.get("TARGET_POLL_SEC", "5"))
TARGET_FILE           = os.environ.get("TARGET_FILE", "/app/data/target_state.json")

_target_value         = TARGET_CURRENT_VALUE  # mutable; can be updated via endpoint
_target_hit           = False                 # True once current_value >= target
_target_hit_at        = None                  # ISO timestamp when first hit
_target_last_value    = None                  # last computed current_value (for /status)
_target_last_check_at = None                  # ISO timestamp of last successful poll
_target_last_error    = None                  # last poll error if any


def fetch_real_wallet_inr():
    """Fetch CoinDCX futures wallet INR balance (Available + Locked).
    Returns float or None on failure. This matches CoinDCX UI's
    'Available + Locked margin' exactly."""
    try:
        resp = _signed_get("/exchange/v1/derivatives/futures/wallets")
        if resp.status_code != 200:
            return None
        wallets = resp.json()
        for w in wallets:
            if w.get("currency_short_name") == "INR":
                bal = float(w.get("balance", 0) or 0)
                locked = float(w.get("locked_balance", 0) or 0)
                return bal + locked
        return None
    except Exception as e:
        log.warning(f"target: wallet fetch failed: {e}")
        return None


def fetch_real_positions_unrealized_inr():
    """Sum unrealized P&L across ALL active CoinDCX positions in INR.
    Orphan-proof: asks CoinDCX directly, not active_trades. Returns float
    or None on failure."""
    try:
        resp = _sign_post(
            "/exchange/v1/derivatives/futures/positions",
            {"page": "1", "size": "100", "margin_currency_short_name": ["INR"]},
        )
        if resp.status_code != 200:
            return None
        positions = resp.json()
        total_inr = 0.0
        for p in positions:
            qty = float(p.get("active_pos", 0) or 0)
            if qty == 0:
                continue
            avg = float(p.get("avg_price", 0) or 0)
            mark = float(p.get("mark_price", 0) or 0)
            rate = float(p.get("settlement_currency_avg_price", 0) or 0)
            if avg <= 0 or mark <= 0 or rate <= 0:
                continue
            direction = 1 if qty > 0 else -1
            unrealized_usdt = direction * (mark - avg) * abs(qty)
            total_inr += unrealized_usdt * rate
        return total_inr
    except Exception as e:
        log.warning(f"target: positions fetch failed: {e}")
        return None


def fetch_real_current_value():
    """Compute the same 'Current value' shown in CoinDCX UI.
    Returns (current_value, wallet_inr, unrealized_inr) or (None, None, None)
    on failure."""
    wallet = fetch_real_wallet_inr()
    if wallet is None:
        return None, None, None
    unrealized = fetch_real_positions_unrealized_inr()
    if unrealized is None:
        return None, wallet, None
    return wallet + unrealized, wallet, unrealized


def load_target_state():
    """Load TARGET state from disk. Survives Railway restarts when volume
    is mounted at TARGET_FILE's parent directory."""
    global _target_value, _target_hit, _target_hit_at
    try:
        with open(TARGET_FILE) as f:
            state = json.load(f)
        _target_value  = float(state.get("target_value", TARGET_CURRENT_VALUE))
        _target_hit    = bool(state.get("hit", False))
        _target_hit_at = state.get("hit_at")
        log.info(f"🎯 Target state loaded: target=₹{_target_value}, "
                 f"hit={_target_hit}, hit_at={_target_hit_at}")
    except FileNotFoundError:
        log.info(f"🎯 No target file at {TARGET_FILE} — using env default ₹{TARGET_CURRENT_VALUE}")
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        log.warning(f"⚠️ Target file corrupt ({e}) — using env default ₹{TARGET_CURRENT_VALUE}")


def save_target_state():
    """Persist current TARGET state to disk."""
    try:
        os.makedirs(os.path.dirname(TARGET_FILE), exist_ok=True)
        with open(TARGET_FILE, "w") as f:
            json.dump({
                "target_value": _target_value,
                "hit": _target_hit,
                "hit_at": _target_hit_at,
            }, f, indent=2)
    except Exception as e:
        log.error(f"❌ Failed to save target state: {e}")


def target_worker():
    """Background thread: every TARGET_POLL_SEC, fetches real wallet balance
    (Available + Locked, INR) from CoinDCX and flips _target_hit if wallet
    reaches target. Self-gates on TARGET_ENABLED and _target_value > 0 so
    it can be paused via env var or by clearing the target.

    WALLET-ONLY MODE: this looks at realized money in your wallet, NOT
    floating unrealized P&L. Trigger fires when actually-banked profit
    crosses target. This avoids the mark-price API drift that affected
    the earlier wallet+unrealized implementation."""
    global _target_hit, _target_hit_at, _target_last_value, _target_last_check_at, _target_last_error
    log.info(f"🎯 Target monitor started — enabled={TARGET_ENABLED}, "
             f"target=₹{_target_value}, poll={TARGET_POLL_SEC}s, "
             f"mode=wallet-only, file={TARGET_FILE}")
    while True:
        try:
            time.sleep(TARGET_POLL_SEC)
            if not TARGET_ENABLED:
                continue
            if _target_value is None or _target_value <= 0:
                continue  # disabled until target set
            wallet = fetch_real_wallet_inr()
            _target_last_check_at = datetime.now(timezone.utc).isoformat()
            if wallet is None:
                _target_last_error = "wallet fetch failed"
                continue
            _target_last_value = wallet
            _target_last_error = None
            # State transition: not hit -> hit
            if not _target_hit and wallet >= _target_value:
                _target_hit = True
                _target_hit_at = datetime.now(timezone.utc).isoformat()
                save_target_state()
                log.info(f"🎯 TARGET HIT: wallet=₹{wallet:.2f} >= "
                         f"target=₹{_target_value:.2f} — "
                         f"new entries/reverses will be rejected")
        except Exception as e:
            _target_last_error = str(e)
            log.error(f"target_worker error: {e}", exc_info=True)


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
    _save_active_trades()
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


# ═══════════════════════════════════════════════════════════
#  BASELINE HELPERS
# ═══════════════════════════════════════════════════════════
def load_baseline_state():
    """Load baseline state from disk. Falls back to in-memory globals if file
    is missing/corrupt. File survives Railway redeploys only if a volume is
    mounted at the BASELINE_FILE's parent directory."""
    global _baseline_inr, _baseline_realized_pnl, _baseline_lock_count
    global _baseline_last_lock_at, _baseline_history
    try:
        with open(BASELINE_FILE) as f:
            state = json.load(f)
        _baseline_inr           = state.get("baseline")
        _baseline_realized_pnl  = float(state.get("realized_pnl", 0.0))
        _baseline_lock_count    = int(state.get("lock_count", 0))
        _baseline_last_lock_at  = state.get("last_lock_at")
        _baseline_history       = state.get("history", [])
        log.info(f"📐 Baseline loaded: ₹{_baseline_inr}, realized=₹{_baseline_realized_pnl:.2f}, "
                 f"locks={_baseline_lock_count}")
    except FileNotFoundError:
        log.info(f"📐 No baseline file at {BASELINE_FILE} — baseline starts uninitialized")
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        log.warning(f"⚠️ Baseline file corrupt ({e}) — starting fresh")


def save_baseline_state():
    """Persist current baseline state to disk."""
    try:
        os.makedirs(os.path.dirname(BASELINE_FILE), exist_ok=True)
        with open(BASELINE_FILE, "w") as f:
            json.dump({
                "baseline": _baseline_inr,
                "realized_pnl": _baseline_realized_pnl,
                "lock_count": _baseline_lock_count,
                "last_lock_at": _baseline_last_lock_at,
                "history": _baseline_history[-50:],  # keep last 50 events
            }, f, indent=2)
    except Exception as e:
        log.error(f"❌ Failed to save baseline: {e}")


def baseline_current_equity():
    """Compute current account equity in INR from internal state.
    = baseline + realized P&L since baseline + unrealized P&L on open positions.
    Returns None if equity can't be computed (e.g. price fetch failed)."""
    if _baseline_inr is None:
        return None
    pnl_open, _, _ = compute_net_roe()
    if pnl_open is None:
        # Price fetch failed — treat unrealized as 0 (conservative for upside check)
        pnl_open = 0.0
    return _baseline_inr + _baseline_realized_pnl + pnl_open


def in_baseline_cooldown():
    """Return True if we're in the post-baseline-trigger cooldown window."""
    return time.time() < _baseline_cooldown_until


def baseline_cooldown_remaining_sec():
    return max(0, int(_baseline_cooldown_until - time.time()))


def trigger_baseline_lock():
    """Called from the worker when current equity crosses target.
    Closes all positions, banks the gain, rolls baseline forward, activates
    cooldown. Updates state file."""
    global _baseline_inr, _baseline_realized_pnl, _baseline_lock_count
    global _baseline_last_lock_at, _baseline_history, _baseline_cooldown_until

    old_baseline = _baseline_inr
    if old_baseline is None or old_baseline <= 0:
        return  # Defensive guard

    equity_at_trigger = baseline_current_equity()
    if equity_at_trigger is None:
        log.warning("⚠️ Baseline trigger aborted — equity unavailable")
        return

    log.info(f"🎯 BASELINE TRIGGER: equity ₹{equity_at_trigger:.2f} ≥ "
             f"target ₹{old_baseline * (1 + BASELINE_TRIGGER_PCT/100):.2f}")

    # Close all positions using the existing profit-lock close machinery.
    # This handles native SLs, market closes, log_trade_event, and clears
    # active_trades. We pass trigger_pct=None so it does NOT bump the
    # daily-cap counter (baseline locks are independent of daily-cap).
    close_all_positions(
        trigger_reason=f"baseline target — equity ₹{equity_at_trigger:.2f} ≥ "
                       f"₹{old_baseline * (1 + BASELINE_TRIGGER_PCT/100):.2f}",
        trigger_pct=None
    )

    # Roll baseline forward
    new_baseline = old_baseline * (1 + BASELINE_ROLLOVER_PCT / 100)
    realized_this_cycle = equity_at_trigger - old_baseline

    _baseline_inr = new_baseline
    _baseline_realized_pnl = 0.0  # reset for new cycle
    _baseline_lock_count += 1
    _baseline_last_lock_at = datetime.now(timezone.utc).isoformat()
    _baseline_history.append({
        "timestamp": _baseline_last_lock_at,
        "old_baseline": old_baseline,
        "trigger_equity": equity_at_trigger,
        "new_baseline": new_baseline,
        "realized_pnl": realized_this_cycle,
    })

    # Activate baseline-specific cooldown (independent of profit-lock cooldown)
    _baseline_cooldown_until = time.time() + BASELINE_COOLDOWN_SEC

    save_baseline_state()
    log.info(f"📈 Baseline rolled: ₹{old_baseline:.2f} → ₹{new_baseline:.2f} "
             f"(banked ₹{(new_baseline - old_baseline) - 0:.2f} buffer of ₹{realized_this_cycle - (new_baseline - old_baseline):.2f}, "
             f"cooldown={BASELINE_COOLDOWN_SEC}s)")


def baseline_record_realized_pnl(symbol, entry_price, exit_price, qty, side, leverage):
    """Add realized P&L from a closed (or partially closed) position to the
    baseline accumulator. Called from book/reverse/close handlers.
    No-op if baseline isn't initialized."""
    global _baseline_realized_pnl
    if _baseline_inr is None:
        return
    try:
        entry = float(entry_price or 0)
        exit_ = float(exit_price or 0)
        q     = float(qty or 0)
        if entry <= 0 or exit_ <= 0 or q <= 0:
            return
        direction = 1 if side == "buy" else -1
        pnl_usd = (exit_ - entry) * q * direction
        pnl_inr = pnl_usd * USDT_INR_RATE
        _baseline_realized_pnl += pnl_inr
        log.info(f"📐 Realized P&L recorded: {symbol} {side} {q} @ {entry}→{exit_} = "
                 f"₹{pnl_inr:+.2f} | total since baseline: ₹{_baseline_realized_pnl:+.2f}")
        save_baseline_state()
    except Exception as e:
        log.warning(f"baseline_record_realized_pnl failed for {symbol}: {e}")


def baseline_worker():
    """Background thread: every POLL_INTERVAL_SEC, checks if baseline target
    has been crossed and fires the lock if so. Self-gates on BASELINE_ENABLED
    so you can flip it via env var without redeploy (after restart)."""
    log.info(f"📐 Baseline monitor started — trigger={BASELINE_TRIGGER_PCT}%, "
             f"rollover={BASELINE_ROLLOVER_PCT}%, cooldown={BASELINE_COOLDOWN_SEC}s, "
             f"poll={POLL_INTERVAL_SEC}s, file={BASELINE_FILE}")
    while True:
        try:
            time.sleep(POLL_INTERVAL_SEC)
            if not BASELINE_ENABLED:
                continue
            if _baseline_inr is None or _baseline_inr <= 0:
                continue  # Not initialized
            if in_baseline_cooldown():
                continue  # Already triggered, in cooldown
            equity = baseline_current_equity()
            if equity is None:
                continue
            target = _baseline_inr * (1 + BASELINE_TRIGGER_PCT / 100)
            if equity >= target:
                trigger_baseline_lock()
        except Exception as e:
            log.error(f"baseline worker error: {e}", exc_info=True)


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
    _save_active_trades()

def clear_active_trade(pair, reason=""):
    old = active_trades.pop(pair, None)
    if old:
        cancel_native_sl(pair)
        log.info(f"🔓 Cleared: {pair} — {reason}")
        _save_active_trades()

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

        # ─── BASELINE COOLDOWN GATE ───────────────────────────
        # After a baseline trigger fires, reject ENTRY and REVERSE webhooks
        # for BASELINE_COOLDOWN_SEC seconds. BOOK and CLOSE are allowed
        # through so any in-flight Pine signals on positions that somehow
        # remained open can still be processed cleanly.
        if in_baseline_cooldown() and alert_type in ("entry", "reverse"):
            remaining = baseline_cooldown_remaining_sec()
            log.info(f"📐 BASELINE COOLDOWN: rejecting {alert_type} for {symbol} — {remaining}s left")
            log_trade_event(symbol, action, alert_type, "BASELINE_COOLDOWN", f"{remaining}s remaining")
            return jsonify({"status": "rejected", "reason": f"baseline cooldown ({remaining}s)"}), 200

        # ─── SL LOCKOUT GATE ──────────────────────────────────
        # If this symbol's SL was hit within SL_LOCKOUT_SEC, reject
        # any new entry/reverse for it. Books and closes pass through
        # so any in-flight Pine signals on still-open positions can
        # resolve cleanly. After the lockout expires, the next Pine
        # entry/reverse for this symbol is accepted normally.
        if alert_type in ("entry", "reverse"):
            locked, remaining = in_sl_lockout(symbol)
            if locked:
                log.info(f"🚫 SL LOCKOUT: rejecting {alert_type} for {symbol} — {remaining}s left")
                log_trade_event(symbol, action, alert_type, "SL_LOCKOUT", f"{remaining}s remaining")
                return jsonify({"status": "rejected",
                                "reason": f"SL lockout on {symbol} ({remaining}s)"}), 200

        # ─── TARGET CURRENT VALUE GATE ────────────────────────
        # If wallet equity has hit TARGET_CURRENT_VALUE, reject all new
        # entries and reverses. Books and closes still pass through so
        # existing positions can resolve cleanly. Trading resumes only
        # when /target/set is called with a higher value (or /target/clear
        # is called explicitly).
        if TARGET_ENABLED and _target_hit and alert_type in ("entry", "reverse"):
            cur = _target_last_value
            cur_str = f"₹{cur:.2f}" if cur is not None else "unknown"
            log.info(f"🎯 TARGET HIT: rejecting {alert_type} for {symbol} — "
                     f"current={cur_str} >= target=₹{_target_value:.2f}")
            log_trade_event(symbol, action, alert_type, "TARGET_HIT",
                            f"current={cur_str} target=₹{_target_value:.2f}")
            return jsonify({"status": "rejected",
                            "reason": f"target ₹{_target_value:.2f} reached "
                                      f"(current={cur_str})"}), 200

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
            _save_active_trades()

            # Record realized P&L for baseline tracking (partial book)
            baseline_record_realized_pnl(
                symbol=symbol,
                entry_price=trade["entry_price"],
                exit_price=coin_price,
                qty=book_qty,
                side=trade["side"],
                leverage=leverage
            )

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

                # Record realized P&L for baseline tracking (full close on SL)
                baseline_record_realized_pnl(
                    symbol=symbol,
                    entry_price=trade["entry_price"],
                    exit_price=coin_price,
                    qty=close_qty,
                    side=trade["side"],
                    leverage=leverage
                )

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

        # ─── CLOSE — kill switch / SL-wait close-only ─────────
        elif alert_type == "close":
            reason = data.get("reason", "unknown")
            ret_pct = data.get("return_pct", "?")
            # Distinguish SL-wait from kill switch in logs
            if reason == "sl_wait":
                log.info(f"⏳ SL-WAIT close: {symbol}")
                # Only arm lockout if we actually held this position. Pine emits
                # sl_wait for every SL on every watched symbol, but the lockout
                # is only meaningful for positions the server was tracking —
                # otherwise we'd block the next legitimate fresh entry on a
                # symbol we never had a position on in the first place.
                if symbol in active_trades:
                    mark_sl_lockout(symbol, reason="Pine sl_wait close on tracked position")
                else:
                    log.info(f"⏳ SL-WAIT for {symbol} ignored for lockout — not in active_trades")
            elif reason == "tp_hit":
                log.info(f"✓ TP HIT: {symbol} — return={ret_pct}%")
            elif reason == "sl_hit":
                log.info(f"✗ SL HIT: {symbol} — return={ret_pct}%")
            elif reason == "timer_expired":
                log.info(f"⏱ TIMER EXIT: {symbol} — return={ret_pct}%")
            elif reason == "kill_switch":
                log.info(f"☠️ KILL SWITCH: {symbol} — return={ret_pct}%")
            else:
                log.info(f"☠️ CLOSE ({reason}): {symbol} — return={ret_pct}%")

            cancel_native_sl(symbol)

            if symbol in active_trades:
                trade = active_trades[symbol]
                close_qty = trade["qty"]
                close_side = "sell" if trade["side"] == "buy" else "buy"

                if close_qty > 0:
                    reason_label = {
                        "sl_wait": "SL-WAIT",
                        "tp_hit": "TP",
                        "sl_hit": "SL",
                        "timer_expired": "TIMER",
                        "kill_switch": "KILL",
                    }.get(reason, reason.upper())
                    log.info(f"🔻 {reason_label} close: "
                             f"{close_side.upper()} {close_qty} {symbol}")
                    result = client.place_order(
                        pair=symbol, side=close_side, order_type="market_order",
                        total_quantity=close_qty, leverage=trade.get("leverage", leverage),
                        margin_currency=trade.get("margin_ccy", margin_ccy)
                    )
                    if isinstance(result, dict) and result.get("status") == "error":
                        log.warning(f"⚠️ Close may have failed: {result.get('message','')}")

                # Record realized P&L for baseline tracking
                baseline_record_realized_pnl(
                    symbol=symbol,
                    entry_price=trade["entry_price"],
                    exit_price=coin_price,
                    qty=close_qty,
                    side=trade["side"],
                    leverage=leverage
                )

                clear_active_trade(symbol, f"{reason}")
                log_trade_event(symbol, close_side,
                                reason if reason in ("sl_wait", "tp_hit", "sl_hit", "timer_expired", "kill_switch") else "close",
                                "FILLED",
                                f"reason={reason}")
            else:
                log.info(f"⚠️ Close for {symbol} but not tracked — no action needed")
                log_trade_event(symbol, action,
                                reason if reason in ("sl_wait", "tp_hit", "sl_hit", "timer_expired", "kill_switch") else "close",
                                "SKIP", "not tracked")

            return jsonify({"status": "closed", "symbol": symbol, "reason": reason}), 200

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
        _save_active_trades()
        return jsonify({"status": "ok", "cleared": count})


# ═══════════════════════════════════════════════════════════
#  SL LOCKOUT ENDPOINTS
# ═══════════════════════════════════════════════════════════
@app.route("/sl-lockout/status", methods=["GET"])
def sl_lockout_status():
    """Returns currently locked symbols with remaining seconds.
    No auth required (read-only, no sensitive info)."""
    now = time.time()
    locked = {}
    for sym, until in list(_sl_lockout.items()):
        remaining = int(until - now)
        if remaining > 0:
            locked[sym] = remaining
        else:
            _sl_lockout.pop(sym, None)
    return jsonify({
        "enabled": SL_LOCKOUT_ENABLED,
        "duration_sec": SL_LOCKOUT_SEC,
        "check_interval_sec": SL_LOCKOUT_CHECK_SEC,
        "locked_count": len(locked),
        "locked": locked,
    }), 200


@app.route("/sl-lockout/clear", methods=["POST", "GET"])
def sl_lockout_clear():
    """Manually clear SL lockouts. ?symbol=X clears one, no arg clears all.
    Requires ?secret=... matching WEBHOOK_SECRET."""
    if WEBHOOK_SECRET and request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    symbol = request.args.get("symbol")
    if symbol:
        existed = _sl_lockout.pop(symbol, None) is not None
        if existed:
            log.info(f"🔓 SL LOCKOUT manually cleared: {symbol}")
        return jsonify({"status": "ok", "cleared": symbol if existed else None})
    else:
        count = len(_sl_lockout)
        _sl_lockout.clear()
        if count:
            log.info(f"🔓 SL LOCKOUT manually cleared all ({count} symbols)")
        return jsonify({"status": "ok", "cleared_count": count})


# ═══════════════════════════════════════════════════════════
#  TARGET CURRENT VALUE ENDPOINTS
# ═══════════════════════════════════════════════════════════
@app.route("/target/status", methods=["GET"])
def target_status():
    """Public read-only status of the target system. Includes the most
    recent computed current_value (from the worker's last poll), so this
    is also a quick way to see real wallet equity."""
    return jsonify({
        "enabled": TARGET_ENABLED,
        "target_value": _target_value,
        "hit": _target_hit,
        "hit_at": _target_hit_at,
        "last_current_value": _target_last_value,
        "last_check_at": _target_last_check_at,
        "last_error": _target_last_error,
        "poll_sec": TARGET_POLL_SEC,
        "distance_to_target": (
            None if _target_last_value is None or _target_value is None or _target_value <= 0
            else round(_target_value - _target_last_value, 2)
        ),
    }), 200


@app.route("/target/set", methods=["POST", "GET"])
def target_set():
    """Set or update the target value. If the new value is above the
    current wallet value, the hit flag is cleared and trading resumes.
    Requires ?value=N&secret=... ."""
    global _target_value, _target_hit, _target_hit_at
    if WEBHOOK_SECRET and request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    raw = request.args.get("value")
    if raw is None:
        return jsonify({"error": "value required, e.g. /target/set?value=2400&secret=..."}), 400
    try:
        new_value = float(raw)
    except ValueError:
        return jsonify({"error": f"value must be numeric, got: {raw}"}), 400
    if new_value < 0:
        return jsonify({"error": "value must be >= 0 (use 0 to disable)"}), 400
    old_value = _target_value
    old_hit   = _target_hit
    _target_value = new_value
    # If the new target is strictly above current measured value (or current
    # is unknown), clear the hit flag so trading can resume.
    cur = _target_last_value
    if cur is None or new_value > cur:
        _target_hit = False
        _target_hit_at = None
    save_target_state()
    log.info(f"🎯 TARGET SET: ₹{old_value} -> ₹{new_value} | "
             f"hit: {old_hit} -> {_target_hit} | "
             f"current={'₹{:.2f}'.format(cur) if cur is not None else 'unknown'}")
    return jsonify({
        "status": "ok",
        "old_value": old_value,
        "new_value": new_value,
        "hit": _target_hit,
        "last_current_value": cur,
    }), 200


@app.route("/target/clear", methods=["POST", "GET"])
def target_clear():
    """Disable the target system entirely (sets value to 0, clears hit flag).
    Requires ?secret=... ."""
    global _target_value, _target_hit, _target_hit_at
    if WEBHOOK_SECRET and request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    old_value = _target_value
    _target_value = 0.0
    _target_hit = False
    _target_hit_at = None
    save_target_state()
    log.info(f"🎯 TARGET CLEARED: was ₹{old_value} -> 0 (disabled)")
    return jsonify({"status": "ok", "old_value": old_value, "new_value": 0.0}), 200


# ═══════════════════════════════════════════════════════════
#  BASELINE ENDPOINTS
# ═══════════════════════════════════════════════════════════
@app.route("/baseline/status", methods=["GET"])
def baseline_status():
    if _baseline_inr is None:
        return jsonify({
            "enabled": BASELINE_ENABLED,
            "baseline": None,
            "message": "Baseline not set. Call /baseline/set?value=N&secret=... to initialize.",
            "trigger_pct": BASELINE_TRIGGER_PCT,
            "rollover_pct": BASELINE_ROLLOVER_PCT,
            "cooldown_sec": BASELINE_COOLDOWN_SEC,
        }), 200
    pnl_open, _, _ = compute_net_roe()
    pnl_open_safe = pnl_open if pnl_open is not None else 0.0
    current_equity = _baseline_inr + _baseline_realized_pnl + pnl_open_safe
    target = _baseline_inr * (1 + BASELINE_TRIGGER_PCT / 100)
    next_target_after_lock = (_baseline_inr * (1 + BASELINE_ROLLOVER_PCT / 100)) * (1 + BASELINE_TRIGGER_PCT / 100)
    return jsonify({
        "enabled": BASELINE_ENABLED,
        "baseline_inr": round(_baseline_inr, 2),
        "realized_pnl_since_baseline": round(_baseline_realized_pnl, 2),
        "unrealized_pnl_open": round(pnl_open_safe, 2) if pnl_open is not None else "unavailable",
        "current_equity": round(current_equity, 2),
        "trigger_pct": BASELINE_TRIGGER_PCT,
        "target_value": round(target, 2),
        "pct_to_target": round((current_equity / target - 1) * 100, 2),
        "rollover_pct": BASELINE_ROLLOVER_PCT,
        "next_baseline_after_lock": round(_baseline_inr * (1 + BASELINE_ROLLOVER_PCT / 100), 2),
        "next_target_after_lock": round(next_target_after_lock, 2),
        "in_cooldown": in_baseline_cooldown(),
        "cooldown_remaining_sec": baseline_cooldown_remaining_sec(),
        "lock_count": _baseline_lock_count,
        "last_lock_at": _baseline_last_lock_at,
        "history_count": len(_baseline_history),
        "history_recent": _baseline_history[-5:],
    }), 200


@app.route("/baseline/set", methods=["POST", "GET"])
def baseline_set():
    if request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    raw = request.args.get("value")
    if raw is None:
        return jsonify({"error": "missing 'value' query param (e.g. ?value=2000)"}), 400
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError("must be positive")
    except ValueError:
        return jsonify({"error": f"invalid value '{raw}'"}), 400

    global _baseline_inr, _baseline_realized_pnl
    old = _baseline_inr
    _baseline_inr = value
    _baseline_realized_pnl = 0.0  # reset since this is a fresh anchor
    save_baseline_state()
    log.info(f"📐 Baseline manually set: ₹{old} → ₹{value} (realized P&L reset to 0)")
    return jsonify({
        "status": "set",
        "baseline_inr": value,
        "previous_baseline": old,
        "trigger_value": round(value * (1 + BASELINE_TRIGGER_PCT / 100), 2),
        "trigger_pct": BASELINE_TRIGGER_PCT,
    }), 200


@app.route("/baseline/reset", methods=["POST", "GET"])
def baseline_reset():
    if request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    global _baseline_inr, _baseline_realized_pnl, _baseline_lock_count
    global _baseline_last_lock_at, _baseline_history, _baseline_cooldown_until
    _baseline_inr = None
    _baseline_realized_pnl = 0.0
    _baseline_lock_count = 0
    _baseline_last_lock_at = None
    _baseline_history = []
    _baseline_cooldown_until = 0.0
    save_baseline_state()
    log.info("📐 Baseline RESET — uninitialized, all history cleared")
    return jsonify({"status": "reset"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok", "positions": len(active_trades),
        "active": list(active_trades.keys()),
        "time": datetime.now().isoformat()
    })

# ─── Startup ──────────────────────────────────────────────────
load_tick_sizes()
_load_active_trades()
log.info("🤖 Trail TP/SL Rev Bot ready — Pine-driven bookings + native SL")

# Initialize daily-cap counter for today's IST date
_check_and_reset_daily_counter()
log.info(f"📅 Daily cap initialized — IST date {_daily_counter_date}, "
         f"cap={DAILY_CAP_PCT}%, enabled={DAILY_CAP_ENABLED}")

# Load baseline state from disk (persists across redeploys if volume mounted)
load_baseline_state()
log.info(f"📐 Baseline initialized — enabled={BASELINE_ENABLED}, "
         f"current=₹{_baseline_inr if _baseline_inr is not None else 'unset'}, "
         f"trigger={BASELINE_TRIGGER_PCT}%, rollover={BASELINE_ROLLOVER_PCT}%")

# Start the profit-lock monitor thread (unconditional — it self-gates on the
# PROFIT_LOCK_ENABLED env var so you can flip it without a redeploy).
_profit_lock_thread = threading.Thread(target=profit_lock_worker, daemon=True)
_profit_lock_thread.start()

# Start the baseline monitor thread (unconditional — self-gates on BASELINE_ENABLED).
_baseline_thread = threading.Thread(target=baseline_worker, daemon=True)
_baseline_thread.start()

# Start the SL lockout monitor thread (unconditional — self-gates on SL_LOCKOUT_ENABLED).
_sl_lockout_thread = threading.Thread(target=sl_lockout_worker, daemon=True)
_sl_lockout_thread.start()

# Load target state from disk (persists across redeploys if volume mounted)
load_target_state()
log.info(f"🎯 Target initialized — enabled={TARGET_ENABLED}, "
         f"value=₹{_target_value}, hit={_target_hit}, poll={TARGET_POLL_SEC}s")

# Start the target monitor thread (unconditional — self-gates on TARGET_ENABLED
# and on _target_value > 0). Polls CoinDCX wallet+positions endpoints directly,
# so it's orphan-proof.
_target_thread = threading.Thread(target=target_worker, daemon=True)
_target_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
