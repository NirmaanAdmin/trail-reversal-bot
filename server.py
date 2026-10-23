"""
server.py
─────────
Zerodha (Kite Connect) OPTIONS executor for the TradingView chassis fleet.
Same contract and safety model as the CoinDCX `server2.py`; the instrument
is a long ITM option on the underlying (buy → CE, sell → PE), NRML carry.

ARCHITECTURE (mirrors server2.py, SERVER_EXITS mode)
────────────────────────────────────────────────────
  TradingView Pine (chassis, e.g. ZScore_Breakout_chassis on NSE:<SYM>)
     │  entry  {action buy|sell, symbol, price, labeled, [flat_only, setup_time]}
     │  close  {reason gate_block|kill_switch|..., symbol}
     ▼
  /webhook ── gates ── flip / skip / flat-only ── BUY ITM CE|PE (NRML)
     ▼
  Server owns every exit:
     ladder_worker      spot-based SL / TP rungs / books / trailing, premium-SL backstop
     profit/loss locks  combined premium P&L → close all + cooldown
     daily cap, streak pause, SL lockout, position cap
     expiry guard       close N days before expiry (market hours only)
     reconcile_worker   tracker ⇄ Kite positions (vanished / shrunk / short / untracked)
     daily flatten      optional (off by default — NRML carries overnight)

WEBHOOK CONTRACT  (POST /webhook — identical to server2.py)
──────────────────────────────────────────────────────────
  entry : {"secret","action":"buy|sell","symbol":"HAVELLS","type":"entry",
           "price":"1520.5","labeled":"1", optional "flat_only":"1","setup_time":<epoch ms>}
           leverage / margin_currency / tp_price / sl_price are accepted and IGNORED
           (the server's ladder is authoritative; options are cash-paid, no leverage).
  close : {"secret","action","symbol","type":"close","reason":"gate_block", ...}
  symbol accepts 'HAVELLS', 'NSE:HAVELLS', 'BAJAJ_AUTO', 'M_M', 'NIFTY1!', 'CNXFINANCE'.

ENTRY RULES
───────────
  labeled == "0"                     → reject (resync / ghost — no chart label)
  flat_only == "1":
     Rule A held either side         → skip
     Rule B setup_time ≤ last exit   → skip (FLAT_ONLY_SETUP_CHECK)
  same side already held             → skip (no stacking)
  opposite side held                 → FLIP: sell current option, confirm flat, buy the other
     leg younger than FLIP_FRESH_GRACE_SEC → skip the flip (anti-whipsaw; options pay spread twice)
  flat                               → position cap → size → buy

SAFETY INVARIANTS
─────────────────
  • Every SELL is clamped to the real long quantity in Kite (reduce-only).
    A stale tracker can never sell more than is held → never writes (shorts) an option.
  • Tracking only changes on confirmed fills (order_history), never on order placement.
  • Closes are never gated by cooldowns / caps.
"""
import os
import json
import math
import time
import logging
import threading
import hmac as _hmac_cmp
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Dict, Tuple

import pytz
import requests
from flask import Flask, request, jsonify

import kite_client as kc
from kite_client import KiteClient
from instruments import load as load_instruments, list_underlyings, by_tradingsymbol
from option_resolver import pick_option
from position_sizer import size_position, get_available_equity
from tradable_symbols import (
    TRADABLE_UNDERLYINGS, INDEX_UNDERLYINGS,
    resolve_underlying, spot_ltp_key,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("bot")

IST = pytz.timezone("Asia/Kolkata")


def _env_bool(k, d):
    return os.environ.get(k, d).strip().lower() in ("1", "true", "yes", "on")


# ═══════════════════════════════════════════════════════════════════════
#  ENV CONFIG
# ═══════════════════════════════════════════════════════════════════════
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "")
PRODUCT          = os.environ.get("KITE_PRODUCT", "NRML").strip().upper()

# Ladder (server-owned exits) — all % on the UNDERLYING SPOT
EXIT_POLL_SEC    = float(os.environ.get("EXIT_POLL_SEC", "5"))
SL_PCT           = float(os.environ.get("SL_PCT", "3.0"))
TP_STEP_PCT      = float(os.environ.get("TP_STEP_PCT", "4.0"))
BOOK_PCT         = float(os.environ.get("BOOK_PCT", "33.0"))
MAX_BOOKS        = int(os.environ.get("MAX_BOOKS", "2"))
LADDER_VERBOSE   = _env_bool("LADDER_VERBOSE", "false")
PREMIUM_SL_PCT   = float(os.environ.get("PREMIUM_SL_PCT", "50.0"))   # 0 = off

# Combined premium-P&L locks
PROFIT_LOCK_ENABLED = _env_bool("PROFIT_LOCK_ENABLED", "true")
PROFIT_LOCK_PCT     = float(os.environ.get("PROFIT_LOCK_PCT", "30.0"))
PROFIT_LOCK_CD      = int(os.environ.get("PROFIT_LOCK_COOLDOWN_SEC", "1800"))
LOSS_LOCK_ENABLED   = _env_bool("LOSS_LOCK_ENABLED", "true")
LOSS_LOCK_PCT       = float(os.environ.get("LOSS_LOCK_PCT", "25.0"))
LOSS_LOCK_CD        = int(os.environ.get("LOSS_LOCK_COOLDOWN_SEC", "1800"))

DAILY_CAP_ENABLED   = _env_bool("DAILY_CAP_ENABLED", "true")
DAILY_CAP_PCT       = float(os.environ.get("DAILY_CAP_PCT", "50.0"))

STREAK_PAUSE_ENABLED = _env_bool("STREAK_PAUSE_ENABLED", "true")
STREAK_PAUSE_COUNT   = int(os.environ.get("STREAK_PAUSE_COUNT", "3"))
STREAK_PAUSE_SEC     = int(os.environ.get("STREAK_PAUSE_SEC", "3600"))

SL_LOCKOUT_ENABLED   = _env_bool("SL_LOCKOUT_ENABLED", "true")
SL_LOCKOUT_SEC       = int(os.environ.get("SL_LOCKOUT_SEC", "600"))

MAX_POSITIONS        = int(os.environ.get("MAX_POSITIONS", "5"))       # 0 = unlimited
EXPIRY_SAFETY_DAYS   = int(os.environ.get("EXPIRY_SAFETY_DAYS", "1"))

# server2 parity
FLAT_ONLY_SETUP_CHECK = _env_bool("FLAT_ONLY_SETUP_CHECK", "true")
FLIP_FRESH_GRACE_SEC  = float(os.environ.get("FLIP_FRESH_GRACE_SEC", "15"))
ORDER_CONFIRM_SEC     = float(os.environ.get("ORDER_CONFIRM_SEC", "8"))
SELL_RETRY_BACKOFF_SEC = float(os.environ.get("SELL_RETRY_BACKOFF_SEC", "30"))

RECONCILE_ENABLED   = _env_bool("RECONCILE_ENABLED", "true")
RECONCILE_SEC       = float(os.environ.get("RECONCILE_SEC", "30"))
RECONCILE_GRACE_SEC = float(os.environ.get("RECONCILE_GRACE_SEC", "15"))
RECONCILE_ADOPT     = _env_bool("RECONCILE_ADOPT", "false")    # adopt untracked long options
RECONCILE_COVER_SHORTS = _env_bool("RECONCILE_COVER_SHORTS", "true")

DAILY_FLATTEN_ENABLED  = _env_bool("DAILY_FLATTEN_ENABLED", "false")   # NRML carries by default
DAILY_FLATTEN_TIME     = os.environ.get("DAILY_FLATTEN_TIME", "15:15").strip()
DAILY_FLATTEN_COOLDOWN_SEC = int(os.environ.get("DAILY_FLATTEN_COOLDOWN_SEC", "120"))

DATA_DIR            = os.environ.get("DATA_DIR", "/app/data")
ACTIVE_TRADES_FILE  = os.environ.get("ACTIVE_TRADES_FILE", f"{DATA_DIR}/active_trades.json")
STREAK_FILE         = os.environ.get("STREAK_FILE",        f"{DATA_DIR}/streak_state.json")
EXITS_FILE          = os.environ.get("EXITS_FILE",         f"{DATA_DIR}/last_exits.json")
FLATTEN_FILE        = os.environ.get("DAILY_FLATTEN_FILE", f"{DATA_DIR}/daily_flatten_state.json")

MKT_OPEN_HM  = (9, 15)
MKT_CLOSE_HM = (15, 30)


# ═══════════════════════════════════════════════════════════════════════
#  AUTH (fail-CLOSED, constant-time)
# ═══════════════════════════════════════════════════════════════════════
if not WEBHOOK_SECRET:
    log.warning("⚠️ WEBHOOK_SECRET is empty — ALL authenticated endpoints will reject.")


def _secret_ok(provided) -> bool:
    if not WEBHOOK_SECRET or not provided:
        return False
    return _hmac_cmp.compare_digest(str(provided), WEBHOOK_SECRET)


def _request_secret() -> Optional[str]:
    return request.headers.get("X-Auth-Secret") or request.args.get("secret")


def _redact(d):
    if not isinstance(d, dict):
        return d
    out = dict(d)
    if "secret" in out:
        out["secret"] = "***"
    return out


# ═══════════════════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════════════════
app  = Flask(__name__)
kite = KiteClient()

# active_trades[underlying] = {
#   tradingsymbol, token, lot_size, tick_size, expiry, option_type CE|PE,
#   side buy|sell (Pine signal side), lots_total, lots_remaining, qty,
#   premium_at_entry, spot_at_entry, entry_time, order_id,
#   ladder_books, ladder_sl_spot, unconfirmed (bool), origin entry|adopted
# }
active_trades: Dict[str, dict] = {}
_trade_lock = threading.RLock()

_symbol_last_exit: Dict[str, float] = {}       # underlying → epoch of last full exit
_flat_only_skips = {"held": 0, "stale_setup": 0}
_flip_guard_stats = {"fresh_skips": 0, "flips": 0, "aborted": 0}
_labeled_rejects = 0
_sell_fail_at: Dict[str, float] = {}

_reconcile = {"last_at": None, "last_error": None, "vanished": 0, "shrunk": 0,
              "adopted": 0, "covered_shorts": 0, "untracked": {}, "shorts": {}, "excess": {}}

_price_cache: Dict[str, dict] = {}             # underlying → {spot, opt, ts}

_profit_lock_until = 0.0
_loss_lock_until   = 0.0
_daily_locked_pct_sum = 0.0
_daily_lock_count = 0
_daily_paused = False
_daily_counter_date: Optional[date] = None

_streak_profit_lock_count = 0
_streak_pause_until = 0.0
_streak_last_lock_at: Optional[str] = None
_streak_history: list = []

_sl_lockout: Dict[str, float] = {}

_flatten_until = 0.0
_flatten_last_date: Optional[str] = None

_boot_done = False
trade_log: list = []
MAX_LOG_ENTRIES = 500

_ladder_last_check_at: Optional[str] = None
_ladder_last_error: Optional[str] = None
_ladder_closes = 0


# ═══════════════════════════════════════════════════════════════════════
#  PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════
def _atomic_write_json(path: str, obj) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        log.warning(f"⚠️ Failed to persist {path}: {e}")


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning(f"⚠️ {path} unreadable ({e}) — using default")
        return default


def _save_active_trades():
    _atomic_write_json(ACTIVE_TRADES_FILE, active_trades)


def _save_exits():
    _atomic_write_json(EXITS_FILE, _symbol_last_exit)


def _load_state():
    global _streak_profit_lock_count, _streak_pause_until, _streak_last_lock_at, _streak_history
    global _flatten_last_date
    data = _read_json(ACTIVE_TRADES_FILE, {})
    if isinstance(data, dict):
        active_trades.update(data)
    log.info(f"📂 Restored {len(active_trades)} active trade(s): {list(active_trades.keys())}")
    ex = _read_json(EXITS_FILE, {})
    if isinstance(ex, dict):
        _symbol_last_exit.update({k: float(v) for k, v in ex.items()})
    st = _read_json(STREAK_FILE, {})
    _streak_profit_lock_count = int(st.get("count", 0))
    _streak_pause_until       = float(st.get("pause_until", 0.0))
    _streak_last_lock_at      = st.get("last_lock_at")
    _streak_history           = st.get("history", [])
    _flatten_last_date        = _read_json(FLATTEN_FILE, {}).get("last_date")


def _save_streak():
    _atomic_write_json(STREAK_FILE, {
        "count": _streak_profit_lock_count, "pause_until": _streak_pause_until,
        "last_lock_at": _streak_last_lock_at, "history": _streak_history[-50:],
    })


# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════
def now_ist() -> datetime:
    return datetime.now(IST)


def market_open() -> bool:
    n = now_ist()
    if n.weekday() >= 5:
        return False
    op = n.replace(hour=MKT_OPEN_HM[0], minute=MKT_OPEN_HM[1], second=0, microsecond=0)
    cl = n.replace(hour=MKT_CLOSE_HM[0], minute=MKT_CLOSE_HM[1], second=0, microsecond=0)
    return op <= n <= cl


def _expiry_to_date(v) -> Optional[date]:
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v).date()
        except Exception:
            return None
    return None


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def log_trade_event(symbol, action, alert_type, result, reason=""):
    trade_log.append({"time": now_ist().strftime("%Y-%m-%d %H:%M:%S"), "symbol": symbol,
                      "action": action, "type": alert_type, "result": result, "reason": reason})
    if len(trade_log) > MAX_LOG_ENTRIES:
        trade_log.pop(0)


def _mark_exit(u: str):
    _symbol_last_exit[u] = time.time()
    _save_exits()


def _clear_trade(u: str, why: str):
    with _trade_lock:
        if active_trades.pop(u, None) is not None:
            log.info(f"🧹 Tracking cleared: {u} ({why})")
            _save_active_trades()
            _mark_exit(u)
            _price_cache.pop(u, None)


# ═══════════════════════════════════════════════════════════════════════
#  DAILY CAP / STREAK / SL LOCKOUT
# ═══════════════════════════════════════════════════════════════════════
def _check_and_reset_daily_counter():
    global _daily_locked_pct_sum, _daily_lock_count, _daily_paused, _daily_counter_date
    today = now_ist().date()
    if _daily_counter_date != today:
        if _daily_counter_date is not None:
            log.info(f"🌅 IST midnight — daily reset (prev {_daily_lock_count} locks, "
                     f"{_daily_locked_pct_sum:.2f}%)")
        _daily_locked_pct_sum = 0.0
        _daily_lock_count = 0
        _daily_paused = False
        _daily_counter_date = today


def _bump_daily_counter(lock_pct: float):
    global _daily_locked_pct_sum, _daily_lock_count, _daily_paused
    if not DAILY_CAP_ENABLED:
        return
    _check_and_reset_daily_counter()
    _daily_locked_pct_sum += abs(lock_pct)
    _daily_lock_count += 1
    log.info(f"📊 Daily: lock #{_daily_lock_count} +{abs(lock_pct):.2f}% → "
             f"{_daily_locked_pct_sum:.2f}% / cap {DAILY_CAP_PCT}%")
    if _daily_locked_pct_sum >= DAILY_CAP_PCT:
        _daily_paused = True
        log.warning("🛑 DAILY CAP REACHED — entries paused until IST midnight")


def daily_cap_active() -> bool:
    if not DAILY_CAP_ENABLED:
        return False
    _check_and_reset_daily_counter()
    return _daily_paused


def in_streak_pause() -> bool:
    global _streak_profit_lock_count, _streak_pause_until
    if _streak_pause_until <= 0:
        return False
    if time.time() < _streak_pause_until:
        return True
    log.info(f"✅ Streak pause cleared (was {_streak_profit_lock_count})")
    _streak_profit_lock_count = 0
    _streak_pause_until = 0.0
    _streak_history.append({"ts": datetime.now(timezone.utc).isoformat(), "event": "pause_expired"})
    _save_streak()
    return False


def streak_pause_remaining_sec() -> int:
    return max(0, int(_streak_pause_until - time.time())) if in_streak_pause() else 0


def _register_profit_lock(trigger_pct=None):
    global _streak_profit_lock_count, _streak_pause_until, _streak_last_lock_at
    _streak_profit_lock_count += 1
    _streak_last_lock_at = datetime.now(timezone.utc).isoformat()
    ev = {"ts": _streak_last_lock_at, "event": "profit_lock",
          "count": _streak_profit_lock_count, "trigger_pct": trigger_pct}
    if STREAK_PAUSE_ENABLED and _streak_profit_lock_count >= STREAK_PAUSE_COUNT:
        _streak_pause_until = time.time() + STREAK_PAUSE_SEC
        log.warning(f"⏸️ STREAK PAUSE: {_streak_profit_lock_count} profit locks — "
                    f"pausing entries {STREAK_PAUSE_SEC // 60}m")
        ev["paused"] = True
    _streak_history.append(ev)
    _save_streak()


def _register_loss_lock():
    global _streak_profit_lock_count, _streak_pause_until
    if _streak_profit_lock_count == 0 and _streak_pause_until <= 0:
        return
    _streak_history.append({"ts": datetime.now(timezone.utc).isoformat(),
                            "event": "loss_lock_reset", "previous_count": _streak_profit_lock_count})
    _streak_profit_lock_count = 0
    _streak_pause_until = 0.0
    _save_streak()


def mark_sl_lockout(u: str, reason: str = ""):
    if not SL_LOCKOUT_ENABLED:
        return
    _sl_lockout[u] = time.time() + SL_LOCKOUT_SEC
    log.info(f"🚫 SL LOCKOUT armed: {u} for {SL_LOCKOUT_SEC // 60}m ({reason})")


def in_sl_lockout(u: str) -> Tuple[bool, int]:
    if not SL_LOCKOUT_ENABLED:
        return False, 0
    rem = int(_sl_lockout.get(u, 0) - time.time())
    if rem <= 0:
        _sl_lockout.pop(u, None)
        return False, 0
    return True, rem


def in_flatten_cooldown() -> bool:
    return time.time() < _flatten_until


# ═══════════════════════════════════════════════════════════════════════
#  PRICES — one bulk LTP call for spots + option premiums
# ═══════════════════════════════════════════════════════════════════════
def _opt_key(t: dict) -> str:
    return f"NFO:{t['tradingsymbol']}"


def fetch_prices(snapshot) -> Tuple[Dict[str, float], Dict[str, float]]:
    """snapshot: [(underlying, trade)]. Returns (spots, opts) keyed by underlying
    and refreshes _price_cache."""
    if not snapshot:
        return {}, {}
    keys = []
    for u, t in snapshot:
        keys.append(spot_ltp_key(u))
        keys.append(_opt_key(t))
    raw = kite.bulk_ltp(keys)
    spots, opts = {}, {}
    now = time.time()
    for u, t in snapshot:
        sk, ok_ = spot_ltp_key(u), _opt_key(t)
        if sk in raw:
            spots[u] = raw[sk]
        if ok_ in raw:
            opts[u] = raw[ok_]
        if u in spots and u in opts:
            _price_cache[u] = {"spot": spots[u], "opt": opts[u], "ts": now}
    return spots, opts


def compute_combined_pnl(max_age: float = None):
    """(net_pnl_inr, cost_inr, net_pct) on premium basis. Uses cached prices
    no older than max_age (default 2×EXIT_POLL_SEC); fetches the rest.
    (None, None, None) if any premium is unavailable."""
    max_age = max_age if max_age is not None else 2 * EXIT_POLL_SEC
    with _trade_lock:
        if not active_trades:
            return 0.0, 0.0, 0.0
        snapshot = [(u, dict(t)) for u, t in active_trades.items()]
    now = time.time()
    stale = [(u, t) for u, t in snapshot
             if u not in _price_cache or now - _price_cache[u]["ts"] > max_age]
    if stale:
        fetch_prices(stale)
    pnl = cost = 0.0
    for u, t in snapshot:
        c = _price_cache.get(u)
        if not c:
            return None, None, None
        entry, qty = _f(t["premium_at_entry"]), int(t["qty"])
        pnl += (c["opt"] - entry) * qty
        cost += entry * qty
    if cost <= 0:
        return pnl, 0.0, 0.0
    return pnl, cost, pnl / cost * 100.0


# ═══════════════════════════════════════════════════════════════════════
#  SELL — reduce-only, fill-confirmed
# ═══════════════════════════════════════════════════════════════════════
def _real_long_qty(tsym: str) -> Optional[int]:
    """Real NET quantity of this option in Kite (any product). None = unknown."""
    pm = kite.nfo_positions()
    if pm is None:
        return None
    return int(pm.get(tsym, {}).get("qty", 0))


def _sell_lots(u: str, lots: int, reason: str = "", ref_price: Optional[float] = None,
               manual: bool = False) -> bool:
    """SELL `lots` of the tracked option on `u`. Reduce-only: clamped to the
    real long qty in Kite. Tracking is updated by the CONFIRMED filled qty.
    Returns True if the tracked position shrank (or was found already flat)."""
    with _trade_lock:
        t = active_trades.get(u)
        if not t or lots <= 0:
            return False
        if not manual and time.time() - _sell_fail_at.get(u, 0) < SELL_RETRY_BACKOFF_SEC:
            return False
        lots = min(lots, int(t["lots_remaining"]))
        lot, tsym = int(t["lot_size"]), t["tradingsymbol"]
        want_qty = lots * lot

        real = _real_long_qty(tsym)
        if real is not None:
            if real <= 0:
                log.warning(f"🧯 {u}: Kite shows {tsym} qty={real} — already flat; "
                            f"clearing tracking ({reason})")
                log_trade_event(u, "sell", reason, "SKIP", "already flat on Kite")
                _clear_trade(u, f"{reason} — already flat")
                return True
            if real < want_qty:
                log.warning(f"🧯 REDUCE-ONLY clamp {u}: want {want_qty}, Kite holds {real}")
                want_qty = (real // lot) * lot
                if want_qty <= 0:
                    want_qty = real   # odd lot remainder — sell what is held
        else:
            log.warning(f"⚠️ positions unavailable — selling tracked {want_qty} {tsym} unclamped")

        t["last_order_at"] = time.time()
        if ref_price is None:
            c = _price_cache.get(u)
            ref_price = c["opt"] if c and time.time() - c["ts"] < 30 else None
        try:
            oid = kite.place_order(tsym, "SELL", want_qty, product=PRODUCT,
                                   ref_price=ref_price, tick=_f(t.get("tick_size"), 0.05))
        except Exception as e:
            _sell_fail_at[u] = time.time()
            log.error(f"❌ SELL {tsym} failed: {e}")
            log_trade_event(u, "sell", reason, "REJECT", str(e)[:200])
            return False

        res = kite.await_order(oid, timeout=ORDER_CONFIRM_SEC)
        filled = res["filled_qty"]
        if res["status"] == "UNKNOWN":
            # Could not read the outcome — re-read the position instead of guessing.
            real_after = _real_long_qty(tsym)
            if real_after is not None:
                filled = max(0, min(want_qty, int(t["qty"]) - real_after))
        if filled <= 0:
            _sell_fail_at[u] = time.time()
            log_trade_event(u, "sell", reason, res["status"], res["message"][:200])
            return False

        _sell_fail_at.pop(u, None)
        left = int(t["qty"]) - filled
        if real is not None:
            left = min(left, real - filled)   # tracker can't exceed what Kite still holds
        t["qty"] = max(0, left)
        t["lots_remaining"] = t["qty"] // lot
        log.info(f"✅ SOLD {filled} {tsym} @ {res['avg_price']} ({reason}) — "
                 f"remaining {t['qty']}")
        log_trade_event(u, "sell", reason, "FILLED", f"qty={filled} px={res['avg_price']}")
        if t["qty"] <= 0:
            log.info(f"🏁 Closed all of {u} ({tsym})")
            _clear_trade(u, reason)
        else:
            _save_active_trades()
        return True


def close_position(u: str, reason: str = "", ref_price: Optional[float] = None,
                   manual: bool = False) -> bool:
    with _trade_lock:
        t = active_trades.get(u)
        if not t:
            return False
        lots = max(1, int(t["lots_remaining"]))
    return _sell_lots(u, lots, reason=reason, ref_price=ref_price, manual=manual)


# ═══════════════════════════════════════════════════════════════════════
#  LADDER — server-owned exits on SPOT
# ═══════════════════════════════════════════════════════════════════════
def _spot_sl_price(side: str, e: float) -> Optional[float]:
    if not e or e <= 0:
        return None
    return e * (1 - SL_PCT / 100.0) if side == "buy" else e * (1 + SL_PCT / 100.0)


def _spot_sl_breached(side, spot, sl) -> bool:
    if not sl or sl <= 0 or not spot or spot <= 0:
        return False
    return spot <= sl if side == "buy" else spot >= sl


def _rungs_crossed(side, spot, e) -> int:
    if e <= 0 or spot <= 0 or TP_STEP_PCT <= 0:
        return 0
    prof = (spot - e) / e if side == "buy" else (e - spot) / e
    return int(math.floor(prof / (TP_STEP_PCT / 100.0))) if prof > 0 else 0


def _ladder_sl_target(side, e, k) -> Optional[float]:
    if k <= 0:
        return _spot_sl_price(side, e)
    step = TP_STEP_PCT / 100.0
    return e * (1 + step * (k - 1)) if side == "buy" else e * (1 - step * (k - 1))


def _ratchet_sl(side, cur, tgt):
    if cur is None:
        return tgt
    if tgt is None:
        return cur
    return max(cur, tgt) if side == "buy" else min(cur, tgt)


def _ladder_step(u: str, spot: float, opt: float):
    global _ladder_closes
    with _trade_lock:
        t = active_trades.get(u)
        if not t:
            return
        side = t["side"]
        e = _f(t.get("spot_at_entry"))
        pe = _f(t.get("premium_at_entry"))
        if e <= 0 or spot <= 0 or int(t.get("lots_remaining", 0)) <= 0:
            return
        cur_sl = t.get("ladder_sl_spot")
        if cur_sl is None:
            cur_sl = _spot_sl_price(side, e)
            t["ladder_sl_spot"] = cur_sl
            _save_active_trades()
            log.info(f"🪜 LADDER armed: {u} {side.upper()} spot_entry={e} SL0={cur_sl:.2f}")
        books = int(t.get("ladder_books", 0))
        orig_lots = int(t.get("lots_total", t["lots_remaining"]))

        # 1. premium backstop
        if pe > 0 and PREMIUM_SL_PCT > 0 and opt > 0:
            drop = (1 - opt / pe) * 100
            if drop >= PREMIUM_SL_PCT:
                log.warning(f"🚨 PREMIUM SL {u}: {opt} is −{drop:.1f}% vs {pe} — closing")
                if close_position(u, "premium_sl", ref_price=opt) and u not in active_trades:
                    mark_sl_lockout(u, f"premium SL @ {opt}")
                    _ladder_closes += 1
                return

        # 2. spot SL / trail
        if _spot_sl_breached(side, spot, cur_sl):
            reason = "spot_sl" if books == 0 else "trail_sl"
            log.warning(f"🛡️ LADDER STOP {u} {side.upper()} spot={spot} SL={cur_sl:.2f} books={books}")
            if close_position(u, reason, ref_price=opt) and u not in active_trades:
                mark_sl_lockout(u, f"{reason} @ {spot}")
                _ladder_closes += 1
            return

        # 3. books + ratchet
        k = _rungs_crossed(side, spot, e)
        if LADDER_VERBOSE:
            prof = ((spot - e) / e if side == "buy" else (e - spot) / e) * 100
            log.info(f"🪜 {u} {side} spot={spot} prof={prof:+.2f}% k={k} books={books}/{MAX_BOOKS} "
                     f"sl={cur_sl:.2f} opt={opt} (entry {pe})")
        target_books = min(MAX_BOOKS, k)
        while books < target_books and u in active_trades:
            t = active_trades[u]
            bl = min(max(1, math.floor(orig_lots * BOOK_PCT / 100.0)), int(t["lots_remaining"]))
            if bl >= int(t["lots_remaining"]):
                # A book may never take the last lot(s) — the remainder always
                # trails (matches the Pine ladder mirror). 1–2 lot positions
                # therefore skip books and run on the trailing SL alone.
                t["ladder_books"] = books = target_books
                _save_active_trades()
                break
            log.info(f"🪜 BOOK #{books + 1}/{MAX_BOOKS}: {u} sell {bl} of {t['lots_remaining']} lots")
            if not _sell_lots(u, bl, reason=f"server_book_{books + 1}", ref_price=opt):
                break
            t = active_trades.get(u)
            if not t:
                return
            t["ladder_books"] = books = books + 1
            _save_active_trades()

        t = active_trades.get(u)
        if not t:
            return
        new_sl = _ratchet_sl(side, cur_sl, _ladder_sl_target(side, e, k))
        if new_sl is not None and new_sl != cur_sl:
            log.info(f"🪜 TRAIL {u}: SL {cur_sl:.2f} → {new_sl:.2f} (k={k}, books={books})")
            t["ladder_sl_spot"] = new_sl
            _save_active_trades()


def _expiry_safety_sweep():
    today = now_ist().date()
    with _trade_lock:
        near = [(u, _expiry_to_date(t.get("expiry"))) for u, t in active_trades.items()]
    for u, exp in near:
        if exp is not None and (exp - today).days <= EXPIRY_SAFETY_DAYS:
            log.warning(f"⏰ EXPIRY guard: {u} expires {exp} — closing")
            if close_position(u, "expiry_safety"):
                log_trade_event(u, "sell", "expiry_safety", "FILLED", str(exp))


def ladder_worker():
    global _ladder_last_check_at, _ladder_last_error
    log.info(f"🪜 Ladder worker: poll={EXIT_POLL_SEC}s SL={SL_PCT}% TP_STEP={TP_STEP_PCT}% "
             f"BOOK={BOOK_PCT}%×{MAX_BOOKS} PREMIUM_SL={PREMIUM_SL_PCT}%")
    while True:
        try:
            time.sleep(EXIT_POLL_SEC)
            if not _boot_done or not market_open():
                continue
            _expiry_safety_sweep()
            with _trade_lock:
                snapshot = [(u, dict(t)) for u, t in active_trades.items()]
            if not snapshot:
                continue
            spots, opts = fetch_prices(snapshot)
            _ladder_last_check_at = datetime.now(timezone.utc).isoformat()
            if not spots and not opts:
                _ladder_last_error = "all LTP fetches failed"
                continue
            _ladder_last_error = None
            for u, _ in snapshot:
                if u in spots and u in opts:
                    try:
                        _ladder_step(u, spots[u], opts[u])
                    except Exception as e:
                        log.error(f"ladder step {u}: {e}", exc_info=True)
        except Exception as e:
            _ladder_last_error = str(e)
            log.error(f"ladder_worker: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════
#  CLOSE-ALL + LOCK WORKERS
# ═══════════════════════════════════════════════════════════════════════
def close_all_positions(trigger_reason="profit lock", trigger_pct=None, lock_type="profit") -> int:
    """Arms the matching cooldown FIRST, then closes every tracked position."""
    global _profit_lock_until, _loss_lock_until, _flatten_until
    if lock_type == "loss":
        _loss_lock_until = time.time() + LOSS_LOCK_CD
    elif lock_type == "profit":
        _profit_lock_until = time.time() + PROFIT_LOCK_CD
    elif lock_type == "flatten":
        _flatten_until = time.time() + DAILY_FLATTEN_COOLDOWN_SEC
    with _trade_lock:
        syms = list(active_trades.keys())
    log.info(f"🔒 {lock_type.upper()} ({trigger_reason}) — closing {len(syms)}")
    closed = 0
    for u in syms:
        try:
            if close_position(u, reason=f"{lock_type}_lock", manual=True) and u not in active_trades:
                closed += 1
        except Exception as e:
            log.error(f"close-all {u}: {e}", exc_info=True)
    if lock_type == "loss":
        _register_loss_lock()
    elif lock_type == "profit":
        if trigger_pct is not None:
            _bump_daily_counter(trigger_pct)
        _register_profit_lock(trigger_pct)
    log.info(f"✅ {lock_type.upper()} done — closed {closed}/{len(syms)}")
    return closed


def lock_worker():
    """Profit + loss lock on combined PREMIUM P&L (one worker, one price read)."""
    log.info(f"🎯 Lock worker: profit {PROFIT_LOCK_PCT}% ({PROFIT_LOCK_ENABLED}) · "
             f"loss −{LOSS_LOCK_PCT}% ({LOSS_LOCK_ENABLED})")
    while True:
        try:
            time.sleep(EXIT_POLL_SEC)
            if not _boot_done or not market_open() or not active_trades:
                continue
            if not (PROFIT_LOCK_ENABLED or LOSS_LOCK_ENABLED):
                continue
            pnl, cost, pct = compute_combined_pnl()
            if pct is None or not cost:
                continue
            now = time.time()
            if PROFIT_LOCK_ENABLED and now >= _profit_lock_until and pct >= PROFIT_LOCK_PCT:
                close_all_positions(f"premium P&L {pct:.2f}% ≥ {PROFIT_LOCK_PCT}%", pct, "profit")
            elif (LOSS_LOCK_ENABLED and now >= _loss_lock_until and now >= _profit_lock_until
                  and pct <= -LOSS_LOCK_PCT):
                close_all_positions(f"premium P&L {pct:.2f}% ≤ −{LOSS_LOCK_PCT}%", None, "loss")
        except Exception as e:
            log.error(f"lock_worker: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════
#  RECONCILE — tracker ⇄ Kite positions
# ═══════════════════════════════════════════════════════════════════════
def reconcile(context="reconcile") -> dict:
    """• tracked but gone on Kite      → clear tracking (manual close / exercise)
       • tracked, Kite holds less     → shrink tracking
       • tracked, Kite holds more     → report (excess), never auto-trade it
       • Kite SHORT on a bot option   → buy to cover (RECONCILE_COVER_SHORTS)
       • untracked long option on a whitelisted underlying → report, or adopt
         (RECONCILE_ADOPT) so the ladder manages it."""
    pm = kite.nfo_positions()
    if pm is None:
        _reconcile["last_error"] = "positions unavailable"
        return {"ok": False, "reason": "positions unavailable"}
    _reconcile["last_error"] = None
    _reconcile["last_at"] = datetime.now(timezone.utc).isoformat()
    actions = []
    now = time.time()
    with _trade_lock:
        tracked_syms = {t["tradingsymbol"]: u for u, t in active_trades.items()}
        for u, t in list(active_trades.items()):
            if now - _f(t.get("last_order_at") or t.get("entry_time")) < RECONCILE_GRACE_SEC:
                continue
            tsym, lot = t["tradingsymbol"], int(t["lot_size"])
            real = int(pm.get(tsym, {}).get("qty", 0))
            if real <= 0:
                _reconcile["vanished"] += 1
                actions.append(f"{u}: {tsym} gone on Kite (real={real}) → cleared")
                log_trade_event(u, "-", context, "VANISHED", f"real={real}")
                _clear_trade(u, f"{context}: not held on Kite")
            elif real < int(t["qty"]):
                _reconcile["shrunk"] += 1
                actions.append(f"{u}: tracked {t['qty']} → {real}")
                t["qty"] = real
                t["lots_remaining"] = real // lot
                t.pop("unconfirmed", None)
                _save_active_trades()
            else:
                if t.pop("unconfirmed", None):
                    t["premium_at_entry"] = pm[tsym]["avg_price"] or t["premium_at_entry"]
                    _save_active_trades()
                    actions.append(f"{u}: unconfirmed fill verified on Kite")
                if real > int(t["qty"]):
                    _reconcile["excess"][u] = real - int(t["qty"])
                else:
                    _reconcile["excess"].pop(u, None)

        shorts, untracked = {}, {}
        for tsym, p in pm.items():
            if p["qty"] < 0:
                shorts[tsym] = p["qty"]
                if RECONCILE_COVER_SHORTS and (tsym in tracked_syms or
                                               _is_bot_symbol(tsym)) and market_open():
                    try:
                        oid = kite.place_order(tsym, "BUY", -p["qty"], product=p["product"] or PRODUCT)
                        r = kite.await_order(oid, ORDER_CONFIRM_SEC)
                        _reconcile["covered_shorts"] += 1
                        actions.append(f"COVER short {tsym} {p['qty']} → {r['status']}")
                        log.error(f"🧨 Covered short option {tsym} qty {p['qty']} → {r['status']}")
                    except Exception as e:
                        actions.append(f"COVER {tsym} failed: {e}")
                continue
            if tsym in tracked_syms or p["product"] != PRODUCT:
                continue
            ins = by_tradingsymbol(kite, tsym)
            if not ins or ins.get("instrument_type") not in ("CE", "PE"):
                continue
            name = ins.get("name")
            if name not in TRADABLE_UNDERLYINGS or name in active_trades:
                continue
            untracked[tsym] = p["qty"]
            if RECONCILE_ADOPT and market_open():
                _adopt(name, ins, p)
                actions.append(f"ADOPTED {name} ← {tsym} {p['qty']}")
        _reconcile["shorts"] = shorts
        _reconcile["untracked"] = {k: v for k, v in untracked.items()
                                   if not any(t["tradingsymbol"] == k for t in active_trades.values())}
    for a in actions:
        log.warning(f"🔁 RECONCILE {a}")
    return {"ok": True, "actions": actions}


_bot_symbols: set = set()   # every option tradingsymbol this process has bought


def _is_bot_symbol(tsym: str) -> bool:
    return tsym in _bot_symbols


def _adopt(name: str, ins: dict, p: dict):
    side = "buy" if ins["instrument_type"] == "CE" else "sell"
    spot = kite.bulk_ltp([spot_ltp_key(name)]).get(spot_ltp_key(name), 0.0)
    lot = int(ins["lot_size"])
    active_trades[name] = {
        "tradingsymbol": ins["tradingsymbol"], "token": int(ins["instrument_token"]),
        "lot_size": lot, "tick_size": _f(ins.get("tick_size"), 0.05),
        "expiry": str(ins["expiry"]), "option_type": ins["instrument_type"], "side": side,
        "lots_total": p["qty"] // lot, "lots_remaining": p["qty"] // lot, "qty": p["qty"],
        "premium_at_entry": p["avg_price"], "spot_at_entry": spot, "entry_time": time.time(),
        "order_id": "adopted", "ladder_books": 0,
        "ladder_sl_spot": _spot_sl_price(side, spot), "origin": "adopted",
    }
    _bot_symbols.add(ins["tradingsymbol"])
    _reconcile["adopted"] += 1
    _save_active_trades()
    log.warning(f"🧲 ADOPTED {name} {side.upper()} {ins['tradingsymbol']} qty={p['qty']} "
                f"(ladder anchored to current spot {spot})")


def reconcile_worker():
    log.info(f"🔁 Reconcile worker: every {RECONCILE_SEC}s grace={RECONCILE_GRACE_SEC}s "
             f"adopt={RECONCILE_ADOPT} cover_shorts={RECONCILE_COVER_SHORTS}")
    while True:
        try:
            time.sleep(RECONCILE_SEC)
            if _boot_done and RECONCILE_ENABLED and kite.kite.access_token:
                reconcile()
        except Exception as e:
            _reconcile["last_error"] = str(e)
            log.error(f"reconcile_worker: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════
#  DAILY FLATTEN (optional) + TOKEN REFRESH
# ═══════════════════════════════════════════════════════════════════════
def _flatten_hm():
    try:
        h, m = DAILY_FLATTEN_TIME.split(":")
        return int(h), int(m)
    except Exception:
        return 15, 15


def daily_flatten_worker():
    global _flatten_last_date
    h, m = _flatten_hm()
    log.info(f"⏰ Daily flatten: enabled={DAILY_FLATTEN_ENABLED} at {h:02d}:{m:02d} IST")
    while True:
        try:
            time.sleep(5)
            if not (_boot_done and DAILY_FLATTEN_ENABLED and market_open()):
                continue
            n = now_ist()
            today = n.date().isoformat()
            if _flatten_last_date == today or (n.hour, n.minute) < (h, m):
                continue
            _flatten_last_date = today
            _atomic_write_json(FLATTEN_FILE, {"last_date": today})
            close_all_positions(f"daily flatten {h:02d}:{m:02d}", None, "flatten")
        except Exception as e:
            log.error(f"daily_flatten_worker: {e}", exc_info=True)


def token_refresh_loop():
    from token_refresh import refresh
    while True:
        try:
            n = now_ist()
            target = n.replace(hour=7, minute=0, second=0, microsecond=0)
            if n >= target:
                target += timedelta(days=1)
            time.sleep((target - n).total_seconds())
            refresh()
            kite.reload_token()
            log.info("⏰ Scheduled token refresh OK")
        except Exception as e:
            log.error(f"Scheduled token refresh failed: {e}", exc_info=True)
            time.sleep(300)


def ensure_valid_token_at_boot():
    if kite.is_token_valid():
        log.info("✅ Existing token is valid")
        return
    log.warning("Cached token missing/expired — running boot-time refresh")
    try:
        from token_refresh import refresh
        refresh()
        kite.reload_token()
        if not kite.is_token_valid():
            raise RuntimeError("Token still invalid after refresh")
        log.info("✅ Boot-time token refresh OK")
    except Exception as e:
        log.error(f"❌ Boot-time token refresh failed: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════
#  ENTRY
# ═══════════════════════════════════════════════════════════════════════
def _open_position(u: str, action: str, pine_price: float) -> dict:
    """Resolve ITM option → size → BUY → confirm fill → track.
    Caller holds _trade_lock and has verified the symbol is flat."""
    sk = spot_ltp_key(u)
    live_spot = kite.bulk_ltp([sk]).get(sk) or pine_price
    if not live_spot or live_spot <= 0:
        return {"status": "rejected", "reason": "no spot price"}
    try:
        c = pick_option(kite, u, live_spot, action)
    except Exception as e:
        log_trade_event(u, action, "entry", "REJECT", f"resolver: {e}")
        return {"status": "rejected", "reason": f"resolver: {e}"}
    tsym, lot = c["tradingsymbol"], int(c["lot_size"])
    tick = _f(c.get("tick_size"), 0.05)
    okey = f"NFO:{tsym}"
    prem = kite.bulk_ltp([okey]).get(okey, 0.0)
    if prem <= 0:
        return {"status": "rejected", "reason": f"no premium LTP for {tsym}"}

    lots = size_position(kite, prem, lot)
    if lots <= 0:
        log_trade_event(u, action, "entry", "REJECT", "insufficient cash for 1 lot")
        return {"status": "skipped", "reason": "insufficient cash (calc < 1 lot)"}
    qty = lots * lot
    try:
        oid = kite.place_order(tsym, "BUY", qty, product=PRODUCT, ref_price=prem, tick=tick)
    except Exception as e:
        log.error(f"❌ BUY {tsym} failed: {e}")
        log_trade_event(u, action, "entry", "REJECT", str(e)[:200])
        return {"status": "rejected", "reason": f"order: {e}"}
    _bot_symbols.add(tsym)

    res = kite.await_order(oid, timeout=ORDER_CONFIRM_SEC)
    filled, unconfirmed = res["filled_qty"], False
    if res["status"] == "UNKNOWN":
        real = _real_long_qty(tsym)
        if real is not None:
            filled = max(0, real)
        else:
            filled, unconfirmed = qty, True   # reconcile verifies after the grace window
    if filled <= 0:
        log_trade_event(u, action, "entry", res["status"], res["message"][:200])
        return {"status": "rejected", "reason": f"order {res['status']}: {res['message']}"}

    fill_px = res["avg_price"] or prem
    flots = filled // lot if filled >= lot else 1
    active_trades[u] = {
        "tradingsymbol": tsym, "token": int(c["instrument_token"]), "lot_size": lot,
        "tick_size": tick, "expiry": str(c["expiry"]),
        "option_type": "CE" if action == "buy" else "PE", "side": action,
        "lots_total": flots, "lots_remaining": flots, "qty": filled,
        "premium_at_entry": float(fill_px), "spot_at_entry": float(live_spot),
        "entry_time": time.time(), "last_order_at": time.time(), "order_id": oid,
        "ladder_books": 0, "ladder_sl_spot": _spot_sl_price(action, live_spot),
        "origin": "entry",
    }
    if unconfirmed:
        active_trades[u]["unconfirmed"] = True
    _save_active_trades()
    log.info(f"🚀 ENTRY {u} {action.upper()} → {tsym} {filled} @ {fill_px} (spot {live_spot})"
             f"{' [UNCONFIRMED]' if unconfirmed else ''}")
    log_trade_event(u, action, "entry", "FILLED", f"{tsym} qty={filled} prem={fill_px}")
    return {"status": "filled" if res["status"] == "COMPLETE" else res["status"].lower(),
            "tradingsymbol": tsym, "qty": filled, "lots": flots, "premium": fill_px,
            "spot": live_spot, "expiry": str(c["expiry"])}


def _entry_gate_reason(u: str) -> Optional[str]:
    """Global/per-symbol gates for ENTRY only. Closes are never gated."""
    if not market_open():
        return "market closed"
    if daily_cap_active():
        return f"daily cap ({_daily_locked_pct_sum:.2f}% ≥ {DAILY_CAP_PCT}%)"
    if in_flatten_cooldown():
        return f"daily-flatten cooldown ({int(_flatten_until - time.time())}s)"
    if time.time() < _profit_lock_until:
        return f"profit-lock cooldown ({int(_profit_lock_until - time.time())}s)"
    if time.time() < _loss_lock_until:
        return f"loss-lock cooldown ({int(_loss_lock_until - time.time())}s)"
    if in_streak_pause():
        return f"streak pause ({_streak_profit_lock_count} locks, {streak_pause_remaining_sec()}s)"
    locked, rem = in_sl_lockout(u)
    if locked:
        return f"SL lockout on {u} ({rem}s)"
    return None


# ═══════════════════════════════════════════════════════════════════════
#  WEBHOOK
# ═══════════════════════════════════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def webhook():
    global _labeled_rejects
    try:
        data = request.get_json(force=True, silent=True)
        if data is None:
            try:
                data = json.loads((request.data or b"{}").decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return jsonify({"status": "rejected", "reason": "invalid JSON body"}), 200
        log.info(f"📩 Webhook: {json.dumps(_redact(data))}")
        if not _secret_ok(data.get("secret") or request.headers.get("X-Auth-Secret")):
            return jsonify({"error": "unauthorized"}), 401

        action     = str(data.get("action", "")).lower()
        alert_type = str(data.get("type", "entry")).lower()
        raw_sym    = str(data.get("symbol", ""))
        price      = _f(data.get("price"))
        flat_only  = str(data.get("flat_only", "0")).strip().lower() in ("1", "true", "yes")
        try:
            setup_ms = int(float(data.get("setup_time") or 0))
        except (TypeError, ValueError, OverflowError):
            setup_ms = 0

        if action not in ("buy", "sell"):
            return jsonify({"status": "rejected", "reason": "invalid action"}), 200
        u = resolve_underlying(raw_sym)
        if not u:
            log_trade_event(raw_sym, action, alert_type, "REJECT", "not in whitelist")
            return jsonify({"status": "rejected",
                            "reason": f"symbol {raw_sym!r} not an F&O underlying "
                                      f"(tradable_symbols.py)"}), 200

        # ─── CLOSE — never gated ───────────────────────────────
        if alert_type == "close":
            reason = str(data.get("reason", "close"))
            with _trade_lock:
                if u not in active_trades:
                    log_trade_event(u, action, "close", "SKIP", f"{reason}: not tracked")
                    return jsonify({"status": "skipped", "reason": "not tracked"}), 200
                if reason == "sl_wait":
                    mark_sl_lockout(u, "Pine sl_wait on tracked position")
                ok = close_position(u, reason=f"pine_{reason}", manual=True)
                still = u in active_trades
            return jsonify({"status": "closed" if ok and not still else "partial" if ok else "failed",
                            "symbol": u, "reason": reason}), 200

        if alert_type != "entry":
            log.info(f"⛔ type {alert_type!r} not handled — server owns books/exits")
            return jsonify({"status": "unknown_type", "type": alert_type}), 200

        # ─── LABELED GATE ──────────────────────────────────────
        if str(data.get("labeled", "1")) == "0":
            _labeled_rejects += 1
            log.info(f"🏷️ UNLABELED entry rejected: {u} {action}")
            log_trade_event(u, action, "entry", "UNLABELED", "no chart label")
            return jsonify({"status": "rejected", "reason": "unlabeled entry (no chart label)"}), 200

        why = _entry_gate_reason(u)
        if why:
            log.info(f"⛔ ENTRY {u} {action}: {why}")
            log_trade_event(u, action, "entry", "GATED", why)
            return jsonify({"status": "rejected", "reason": why}), 200

        with _trade_lock:
            held = active_trades.get(u)

            # ─── FLAT-ONLY ─────────────────────────────────────
            if flat_only:
                if held is not None:
                    _flat_only_skips["held"] += 1
                    log_trade_event(u, action, "entry", "SKIP", f"flat-only: holding {held['side']}")
                    return jsonify({"status": "skipped",
                                    "reason": f"flat-only: {u} already held ({held['side']})"}), 200
                last_exit = _symbol_last_exit.get(u)
                if FLAT_ONLY_SETUP_CHECK and last_exit and setup_ms > 0:
                    now_ms = time.time() * 1000.0
                    if setup_ms > now_ms + 60_000 or setup_ms < 1_000_000_000_000:
                        log.warning(f"⚠️ flat-only: implausible setup_time {setup_ms} — rule B skipped")
                    elif setup_ms <= last_exit * 1000.0:
                        _flat_only_skips["stale_setup"] += 1
                        log_trade_event(u, action, "entry", "SKIP", "flat-only: setup before last exit")
                        return jsonify({"status": "skipped",
                                        "reason": "flat-only: setup formed before the last exit"}), 200

            # ─── HELD ──────────────────────────────────────────
            if held is not None:
                if held["side"] == action:
                    mins = int((time.time() - _f(held.get("entry_time"))) // 60)
                    log_trade_event(u, action, "entry", "SKIP", f"already {action} ({mins}m)")
                    return jsonify({"status": "skipped", "reason": "already active (same direction)"}), 200

                age = time.time() - _f(held.get("entry_time"))
                if FLIP_FRESH_GRACE_SEC > 0 and 0 <= age < FLIP_FRESH_GRACE_SEC:
                    _flip_guard_stats["fresh_skips"] += 1
                    log.info(f"⏱️ SKIP flip {u}: leg only {age:.1f}s old (< {FLIP_FRESH_GRACE_SEC:.0f}s)")
                    log_trade_event(u, action, "entry", "SKIP", f"flip on fresh leg ({age:.1f}s)")
                    return jsonify({"status": "skipped", "reason": f"flip on fresh leg ({age:.1f}s)"}), 200

                prev = held["side"]
                log.info(f"🔄 FLIP {u}: {prev.upper()}→{action.upper()} (closing {held['tradingsymbol']})")
                close_position(u, reason=f"flip_{prev}_to_{action}", manual=True)
                if u in active_trades:
                    _flip_guard_stats["aborted"] += 1
                    log.error(f"❌ FLIP ABORT {u}: old leg not fully closed — keeping it tracked")
                    log_trade_event(u, action, "flip_entry", "ABORT", "close not filled")
                    return jsonify({"status": "aborted",
                                    "reason": "flip close not filled; old leg still managed"}), 200
                _flip_guard_stats["flips"] += 1
                res = _open_position(u, action, price)
                res["flipped_from"] = prev
                return jsonify(res), 200

            # ─── FLAT → POSITION CAP → OPEN ────────────────────
            if MAX_POSITIONS > 0 and len(active_trades) >= MAX_POSITIONS:
                log_trade_event(u, action, "entry", "POS_CAP", f"{len(active_trades)}/{MAX_POSITIONS}")
                return jsonify({"status": "rejected",
                                "reason": f"position cap ({len(active_trades)}/{MAX_POSITIONS})"}), 200
            return jsonify(_open_position(u, action, price)), 200

    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 200


# ═══════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════
def _auth():
    return _secret_ok(_request_secret())


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "ts": time.time(), "boot_done": _boot_done,
                    "active_count": len(active_trades), "market_open": market_open()})


@app.route("/status", methods=["GET"])
def status():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    _check_and_reset_daily_counter()
    pnl, cost, pct = compute_combined_pnl()
    with _trade_lock:
        trades = {u: {k: t.get(k) for k in (
            "tradingsymbol", "option_type", "side", "lots_total", "lots_remaining", "qty",
            "premium_at_entry", "spot_at_entry", "ladder_sl_spot", "ladder_books", "expiry",
            "origin", "unconfirmed")} | {
            "age_min": int((time.time() - _f(t.get("entry_time"))) / 60),
            "spot_now": (_price_cache.get(u) or {}).get("spot"),
            "premium_now": (_price_cache.get(u) or {}).get("opt")}
            for u, t in active_trades.items()}
    return jsonify({
        "active_trades": trades, "active_count": len(trades),
        "combined_pnl_inr": pnl, "combined_cost_inr": cost, "combined_pct": pct,
        "locks": {"profit_cooldown_sec": max(0, int(_profit_lock_until - time.time())),
                  "loss_cooldown_sec": max(0, int(_loss_lock_until - time.time())),
                  "flatten_cooldown_sec": max(0, int(_flatten_until - time.time())),
                  "daily_locked_pct": _daily_locked_pct_sum, "daily_lock_count": _daily_lock_count,
                  "daily_paused": _daily_paused, "streak_count": _streak_profit_lock_count,
                  "streak_pause_sec": streak_pause_remaining_sec(),
                  "sl_lockouts": {u: int(v - time.time()) for u, v in _sl_lockout.items()
                                  if v > time.time()}},
        "flat_only": {"setup_check": FLAT_ONLY_SETUP_CHECK, "skips": _flat_only_skips,
                      "last_exits": {u: datetime.fromtimestamp(v, IST).isoformat()
                                     for u, v in _symbol_last_exit.items()}},
        "flip_guard": {"fresh_grace_sec": FLIP_FRESH_GRACE_SEC, **_flip_guard_stats},
        "labeled_rejects": _labeled_rejects,
        "reconcile": {"enabled": RECONCILE_ENABLED, "adopt": RECONCILE_ADOPT, **_reconcile},
        "ladder": {"last_check": _ladder_last_check_at, "last_error": _ladder_last_error,
                   "closes": _ladder_closes},
        "config": {"product": PRODUCT, "order_type": kc.ORDER_TYPE,
                   "limit_buffer_pct": kc.LIMIT_BUFFER_PCT, "market_protection": kc.MARKET_PROTECTION,
                   "static_ip_proxy": bool(kc.proxy_url()), "SL_PCT": SL_PCT,
                   "TP_STEP_PCT": TP_STEP_PCT, "BOOK_PCT": BOOK_PCT, "MAX_BOOKS": MAX_BOOKS,
                   "PREMIUM_SL_PCT": PREMIUM_SL_PCT, "PROFIT_LOCK_PCT": PROFIT_LOCK_PCT,
                   "LOSS_LOCK_PCT": LOSS_LOCK_PCT, "DAILY_CAP_PCT": DAILY_CAP_PCT,
                   "MAX_POSITIONS": MAX_POSITIONS, "EXPIRY_SAFETY_DAYS": EXPIRY_SAFETY_DAYS,
                   "daily_flatten": f"{DAILY_FLATTEN_TIME} ({'on' if DAILY_FLATTEN_ENABLED else 'off'})",
                   "fixed_premium_inr": os.environ.get("FIXED_PREMIUM_INR", "0"),
                   "equity_pct": os.environ.get("EQUITY_PCT_PER_TRADE", "20")},
        "market_open": market_open(), "now_ist": now_ist().isoformat(),
    })


@app.route("/egress-ip", methods=["GET"])
def egress_ip():
    """The IP Kite sees for this server's API calls — register THIS on the Kite app."""
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    try:
        r = requests.get("https://api.ipify.org?format=json", proxies=kc.proxies(), timeout=8)
        return jsonify({"egress_ip": r.json().get("ip"), "via_proxy": bool(kc.proxy_url())})
    except Exception as e:
        return jsonify({"error": str(e), "via_proxy": bool(kc.proxy_url())}), 502


@app.route("/equity", methods=["GET"])
def equity_endpoint():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"available_inr": get_available_equity(kite)})


@app.route("/reconcile", methods=["GET", "POST"])
def reconcile_endpoint():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(reconcile("manual"))


@app.route("/close-all", methods=["POST"])
def close_all_endpoint():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"closed": close_all_positions("manual close-all", None, "manual")})


@app.route("/close-one", methods=["POST"])
def close_one_endpoint():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    payload = request.get_json(force=True, silent=True) or {}
    u = resolve_underlying(payload.get("symbol") or request.args.get("symbol") or "")
    if not u:
        return jsonify({"error": "unknown symbol"}), 400
    ok = close_position(u, reason="manual", manual=True)
    return jsonify({"closed": ok and u not in active_trades, "underlying": u})


@app.route("/clear-tracking", methods=["POST"])
def clear_tracking():
    """Drop a symbol from tracking WITHOUT trading (position handled by hand)."""
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    payload = request.get_json(force=True, silent=True) or {}
    u = resolve_underlying(payload.get("symbol") or request.args.get("symbol") or "")
    if not u or u not in active_trades:
        return jsonify({"cleared": False, "underlying": u}), 200
    _clear_trade(u, "manual clear-tracking")
    return jsonify({"cleared": True, "underlying": u})


@app.route("/locks/clear", methods=["POST"])
def clear_locks():
    global _profit_lock_until, _loss_lock_until, _flatten_until
    global _daily_paused, _daily_locked_pct_sum, _daily_lock_count
    global _streak_profit_lock_count, _streak_pause_until
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    _profit_lock_until = _loss_lock_until = _flatten_until = 0.0
    _daily_paused, _daily_locked_pct_sum, _daily_lock_count = False, 0.0, 0
    _streak_profit_lock_count, _streak_pause_until = 0, 0.0
    _sl_lockout.clear()
    _save_streak()
    return jsonify({"ok": True})


@app.route("/daily-flatten/force", methods=["POST"])
def daily_flatten_force():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"closed": close_all_positions("forced flatten", None, "flatten")})


@app.route("/refresh-instruments", methods=["POST"])
def refresh_instruments_endpoint():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"loaded": len(load_instruments(kite, force=True))})


@app.route("/underlyings", methods=["GET"])
def underlyings_endpoint():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    k = list_underlyings(kite)
    return jsonify({"kite_count": len(k), "whitelist_count": len(TRADABLE_UNDERLYINGS),
                    "in_kite_not_whitelist": sorted(set(k) - TRADABLE_UNDERLYINGS),
                    "in_whitelist_not_kite": sorted(TRADABLE_UNDERLYINGS - set(k))})


@app.route("/stats", methods=["GET"])
def stats():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"log_count": len(trade_log), "events": trade_log[-100:]})


# ═══════════════════════════════════════════════════════════════════════
#  BOOT
# ═══════════════════════════════════════════════════════════════════════
def boot():
    global _boot_done, _daily_counter_date
    log.info("=" * 70)
    log.info(" ZERODHA OPTIONS — server2 contract | server-owned exits | NRML ")
    log.info("=" * 70)
    log.info(f"Ladder SL={SL_PCT}% TP_STEP={TP_STEP_PCT}% BOOK={BOOK_PCT}%×{MAX_BOOKS} "
             f"PREMIUM_SL={PREMIUM_SL_PCT}% poll={EXIT_POLL_SEC}s")
    log.info(f"Orders {kc.ORDER_TYPE} (buffer {kc.LIMIT_BUFFER_PCT}%, protection "
             f"{kc.MARKET_PROTECTION}) product={PRODUCT} proxy={'yes' if kc.proxy_url() else 'NO'}")
    log.info(f"Whitelist {len(TRADABLE_UNDERLYINGS)} ({len(INDEX_UNDERLYINGS)} indices) "
             f"MAX_POSITIONS={MAX_POSITIONS}")
    _daily_counter_date = now_ist().date()
    ensure_valid_token_at_boot()
    _load_state()
    for t in active_trades.values():
        _bot_symbols.add(t["tradingsymbol"])
    try:
        load_instruments(kite, force=True)
    except Exception as e:
        log.error(f"instruments load failed at boot: {e}")
    for name, fn in (("ladder", ladder_worker), ("locks", lock_worker),
                     ("reconcile", reconcile_worker), ("daily_flatten", daily_flatten_worker)):
        threading.Thread(target=fn, daemon=True, name=name).start()
    if os.environ.get("KITE_TOTP_SECRET"):
        threading.Thread(target=token_refresh_loop, daemon=True, name="token_refresh").start()
    else:
        log.warning("KITE_TOTP_SECRET not set — daily token refresh disabled")
    _boot_done = True
    log.info("✅ Boot complete")


boot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
