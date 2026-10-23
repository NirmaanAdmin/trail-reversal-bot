"""
option_resolver.py
──────────────────
Translate a (underlying, spot, side) signal into a specific option contract.

Default strategy rules:
  - side="buy"  → BUY a CE with strike AT OR JUST BELOW spot   (ITM call)
  - side="sell" → BUY a PE with strike AT OR JUST ABOVE spot   (ITM put)

STRIKE_OFFSET shifts this away from ITM:
   0 = ITM-by-one (default)            CE just below spot, PE just above
   1 = ATM                              one strike further OTM
   2 = OTM-by-one                       two strikes further OTM
  -1 = ITM-by-two                       one strike deeper ITM
etc. Negative goes deeper ITM; positive goes toward OTM.

Expiry selection:
  - Nearest expiry that's at least EXPIRY_SAFETY_DAYS away
  - Stock options are physically settled → safety days non-negotiable
  - Indices are cash-settled (NIFTY/BANKNIFTY weekly etc.) but same rule applies
"""
import os
import logging
from datetime import date, datetime
from typing import Optional

import pytz

from instruments import chain_for

log = logging.getLogger("kite.resolver")

EXPIRY_SAFETY_DAYS = int(os.environ.get("EXPIRY_SAFETY_DAYS", "1"))
STRIKE_OFFSET      = int(os.environ.get("STRIKE_OFFSET", "0"))   # 0 = ITM-by-one


def pick_option(kite_client, underlying: str, spot: float, side: str) -> dict:
    """Resolve the option contract to BUY for this signal.

    Returns the full instrument dict from the Kite dump (tradingsymbol,
    instrument_token, lot_size, strike, expiry, etc.).
    Raises RuntimeError if no acceptable contract exists.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy/sell, got {side!r}")
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")

    opt_type = "CE" if side == "buy" else "PE"
    today = datetime.now(pytz.timezone("Asia/Kolkata")).date()

    candidates = chain_for(kite_client, underlying, opt_type)
    candidates = [c for c in candidates if c.get("expiry") and c["expiry"] >= today]
    if not candidates:
        raise RuntimeError(
            f"No {opt_type} contracts found for {underlying!r} "
            f"(check spelling; must match Kite's instrument.name)"
        )

    # 1. Pick expiry — nearest at least EXPIRY_SAFETY_DAYS away
    expiries = sorted({c["expiry"] for c in candidates})
    chosen_expiry: Optional[date] = None
    for e in expiries:
        if (e - today).days > EXPIRY_SAFETY_DAYS:
            chosen_expiry = e
            break
    if chosen_expiry is None:
        raise RuntimeError(
            f"All {underlying} {opt_type} expiries within "
            f"EXPIRY_SAFETY_DAYS={EXPIRY_SAFETY_DAYS} — refusing. "
            f"Available: {expiries}"
        )

    chain = sorted(
        [c for c in candidates if c["expiry"] == chosen_expiry],
        key=lambda c: c["strike"],
    )
    if not chain:
        raise RuntimeError(f"Empty chain {underlying} {opt_type} on {chosen_expiry}")

    # 2. Pick strike — base ITM choice, then shift by STRIKE_OFFSET
    if side == "buy":
        # Long CE: base = highest strike ≤ spot
        below = [c for c in chain if c["strike"] <= spot]
        if not below:
            base_idx = 0
        else:
            base_idx = chain.index(below[-1])
        # STRIKE_OFFSET > 0 moves OUT of money (higher strike for CE)
        target_idx = base_idx + STRIKE_OFFSET
    else:
        # Long PE: base = lowest strike ≥ spot
        above = [c for c in chain if c["strike"] >= spot]
        if not above:
            base_idx = len(chain) - 1
        else:
            base_idx = chain.index(above[0])
        # STRIKE_OFFSET > 0 moves OUT of money (lower strike for PE)
        target_idx = base_idx - STRIKE_OFFSET

    target_idx = max(0, min(target_idx, len(chain) - 1))
    chosen = chain[target_idx]

    log.info(
        f"🎯 Picked {chosen['tradingsymbol']} | strike={chosen['strike']} "
        f"expiry={chosen['expiry']} lot={chosen['lot_size']} | "
        f"for {underlying} {side} @ spot {spot} (offset={STRIKE_OFFSET})"
    )
    return chosen
