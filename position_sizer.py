"""
position_sizer.py
─────────────────
20%-of-equity position sizing for option BUYING.

Option buying uses cash, not margin (you pay full premium upfront). Sizing
is just: target_premium_budget / (option_LTP × lot_size).

We pull AVAILABLE equity at every sizing call (not initial deposit), so:
  - First trade: 20% of full equity
  - Second concurrent trade: 20% of what's LEFT after the first
  - Naturally caps concurrent positions at ~4-5 before sizes shrink below
    LOTS_MIN and trades start getting rejected
  - MAX_POSITIONS env var imposes an additional hard ceiling

Returns 0 → server rejects with "insufficient equity" — don't take
undersized positions, wait for capital to free up.
"""
import os
import math
import logging

log = logging.getLogger("kite.sizer")

EQUITY_PCT = float(os.environ.get("EQUITY_PCT_PER_TRADE", "20.0")) / 100.0
LOTS_MIN   = int(os.environ.get("NUM_LOTS_MIN", "1"))
LOTS_MAX   = int(os.environ.get("NUM_LOTS_MAX", "100"))
# Fixed premium budget per trade (INR). > 0 overrides EQUITY_PCT_PER_TRADE —
# same role as FIXED_MARGIN_INR on the CoinDCX server. The order is still
# refused if available cash cannot cover the computed premium + buffer.
FIXED_PREMIUM_INR = float(os.environ.get("FIXED_PREMIUM_INR", "0"))
CASH_BUFFER_INR   = float(os.environ.get("CASH_BUFFER_INR", "100"))


def get_available_equity(kite_client) -> float:
    """Best-effort read of CURRENTLY available INR equity from Kite."""
    try:
        m = kite_client.margins(segment="equity")
    except Exception as e:
        log.error(f"margins() failed: {e}")
        return 0.0

    if not isinstance(m, dict):
        log.error(f"margins() returned non-dict: {type(m)}")
        return 0.0

    avail = m.get("available", {}) or {}
    # live_balance is the most reliable single number — already nets utilised margin
    lb = float(avail.get("live_balance", 0) or 0)
    if lb > 0:
        return lb
    # Fallback
    cash  = float(avail.get("cash", 0) or 0)
    coll  = float(avail.get("collateral", 0) or 0)
    payin = float(avail.get("intraday_payin", 0) or 0)
    fallback = cash + coll + payin
    log.warning(f"live_balance unavailable; using fallback cash+coll+payin = {fallback}")
    return fallback


def size_position(kite_client, option_ltp: float, lot_size: int) -> int:
    """Returns number of lots to buy, or 0 if insufficient equity."""
    if option_ltp <= 0:
        log.error(f"Bad option_ltp={option_ltp}")
        return 0
    if lot_size <= 0:
        log.error(f"Bad lot_size={lot_size}")
        return 0

    equity = get_available_equity(kite_client)
    if equity <= 0:
        log.error("Available equity is 0 — cannot size")
        return 0

    budget = FIXED_PREMIUM_INR if FIXED_PREMIUM_INR > 0 else equity * EQUITY_PCT
    per_lot_cost = option_ltp * lot_size
    lots_raw = budget / per_lot_cost
    lots = int(math.floor(lots_raw))
    # Never spend more than the cash actually available (minus buffer)
    afford = int(math.floor(max(0.0, equity - CASH_BUFFER_INR) / per_lot_cost))
    if lots > afford:
        log.warning(f"Sizer: budget wants {lots} lots but cash affords {afford}")
        lots = afford

    log.info(
        f"Sizer: equity=₹{equity:,.0f} budget=₹{budget:,.0f} "
        f"({'fixed' if FIXED_PREMIUM_INR > 0 else f'{EQUITY_PCT*100:.0f}%'}) "
        f"per_lot=₹{per_lot_cost:,.0f} → {lots_raw:.2f} → {lots} lots"
    )

    lots = min(lots, LOTS_MAX)
    if lots < LOTS_MIN:
        log.warning(
            f"Sizer rejected: {lots} < LOTS_MIN={LOTS_MIN} "
            f"(insufficient equity for even one lot)"
        )
        return 0
    return lots
