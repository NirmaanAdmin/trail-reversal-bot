"""
instruments.py
──────────────
NFO instruments cache. Pulled once at startup, refreshed every 6 hours.

Each instrument record from Kite has:
  - instrument_token : int     unique numeric ID (used in LTP fetches)
  - tradingsymbol    : str     e.g. "TATASTEEL25JUN200CE"
  - name             : str     UNDERLYING name, e.g. "TATASTEEL"
  - expiry           : date    Python date object
  - strike           : float   strike price (0 for futures)
  - lot_size         : int     contract multiplier
  - instrument_type  : str     "CE" / "PE" / "FUT"
  - segment          : str     "NFO-OPT" for options, "NFO-FUT" for futures
  - exchange         : str     "NFO"
  - tick_size        : float
"""
import time
import logging
from datetime import date

log = logging.getLogger("kite.instruments")

_cache = {"data": [], "ts": 0.0, "by_tsym": {}}
_TTL = 6 * 3600  # 6 hours


def load(kite_client, force: bool = False) -> list:
    """Returns the cached NFO instruments list. Refreshes if stale or forced."""
    if not force and _cache["data"] and time.time() - _cache["ts"] < _TTL:
        return _cache["data"]
    log.info("Fetching NFO instruments dump from Kite...")
    rows = kite_client.instruments("NFO")
    _cache["data"] = rows
    _cache["by_tsym"] = {r.get("tradingsymbol"): r for r in rows}
    _cache["ts"] = time.time()
    opts = sum(1 for r in rows if r.get("segment") == "NFO-OPT")
    futs = sum(1 for r in rows if r.get("segment") == "NFO-FUT")
    log.info(f"Loaded {len(rows)} NFO instruments ({opts} options, {futs} futures)")
    return rows


def chain_for(kite_client, name: str, opt_type: str, expiry: date = None) -> list:
    """Filter the dump to a specific option chain.

    name     - underlying name (e.g. 'TATASTEEL'). Must match instruments.name.
    opt_type - 'CE' or 'PE'
    expiry   - optional date filter; if omitted returns all expiries.
    """
    rows = load(kite_client)
    out = []
    for r in rows:
        if r.get("name") != name:
            continue
        if r.get("instrument_type") != opt_type:
            continue
        if r.get("segment") != "NFO-OPT":
            continue
        if expiry is not None and r.get("expiry") != expiry:
            continue
        out.append(r)
    return out


def list_underlyings(kite_client) -> list:
    """Sorted unique list of underlying names that have option contracts.
    Useful for /underlyings diagnostics."""
    rows = load(kite_client)
    names = {
        r["name"] for r in rows
        if r.get("segment") == "NFO-OPT" and r.get("name")
    }
    return sorted(names)


def by_tradingsymbol(kite_client, tradingsymbol: str):
    """Instrument row for an NFO tradingsymbol, or None."""
    load(kite_client)
    return _cache["by_tsym"].get(tradingsymbol)
