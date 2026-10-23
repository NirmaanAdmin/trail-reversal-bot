"""
kite_client.py
──────────────
Thin wrapper around the KiteConnect SDK (kiteconnect >= 5.2.2).

  - Access token lazily reloaded from disk; one-shot retry on TokenException
  - STATIC-IP EGRESS: every Kite REST call can be routed through a fixed-IP
    proxy (KITE_PROXY_URL). Since 1 Apr 2026 Zerodha rejects order placement
    from IPs not registered on the Kite Connect app (SEBI retail-algo rules).
    Railway egress IPs are dynamic, so on Railway a static-IP proxy is required.
  - Orders: MARKET (with mandatory `market_protection`) or marketable LIMIT
    (LTP ± buffer, tick-rounded). Every order carries a `tag` so the bot's
    own orders are identifiable in the order book.
  - Fill confirmation returns status + filled qty + avg price; an order still
    OPEN after the confirm window is cancelled and its partial fill reported.
  - Positions map (NFO, by tradingsymbol) for reduce-only clamps + reconcile.
"""
import os
import math
import time
import logging
from typing import Optional, Dict, List

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

log = logging.getLogger("kite.client")

API_KEY    = os.environ.get("KITE_API_KEY", "")
TOKEN_PATH = os.environ.get("ACCESS_TOKEN_PATH", "/app/data/kite_access_token.txt")

# Order behaviour
ORDER_TYPE        = os.environ.get("ORDER_TYPE", "LIMIT").strip().upper()   # LIMIT | MARKET
LIMIT_BUFFER_PCT  = float(os.environ.get("LIMIT_BUFFER_PCT", "2.0"))        # marketable-limit buffer
MARKET_PROTECTION = float(os.environ.get("MARKET_PROTECTION", "-1"))        # -1 = Kite auto; 1..100 = %
ORDER_TAG         = os.environ.get("ORDER_TAG", "zsc_opt")[:20]
ALGO_ID           = os.environ.get("KITE_ALGO_ID", "").strip() or None


def proxy_url() -> str:
    """Static-IP egress proxy. KITE_PROXY_URL wins; common add-on vars as fallback."""
    for k in ("KITE_PROXY_URL", "QUOTAGUARDSHIELD_URL", "QUOTAGUARDSTATIC_URL", "FIXIE_URL"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


def proxies() -> Optional[dict]:
    u = proxy_url()
    return {"http": u, "https": u} if u else None


def round_to_tick(price: float, tick: float, up: bool) -> float:
    tick = tick if tick and tick > 0 else 0.05
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(max(tick, n * tick), 2)


class KiteClient:
    def __init__(self):
        if not API_KEY:
            raise RuntimeError("KITE_API_KEY env var is required")
        px = proxies()
        self.kite = KiteConnect(api_key=API_KEY, proxies=px, timeout=10)
        if px:
            log.info("🌐 Kite REST egress via static-IP proxy (KITE_PROXY_URL set)")
        else:
            log.warning("🌐 No KITE_PROXY_URL — orders will be REJECTED unless this "
                        "host's outbound IP is registered on the Kite app")
        self._load_token(required=False)

    # ─── Token ───────────────────────────────────────────────────────
    def _load_token(self, required: bool = True) -> bool:
        try:
            with open(TOKEN_PATH) as f:
                tok = f.read().strip()
            if not tok:
                raise RuntimeError("Token file is empty")
            self.kite.set_access_token(tok)
            log.info(f"Loaded access_token from {TOKEN_PATH}")
            return True
        except FileNotFoundError:
            msg = f"{TOKEN_PATH} not found — run token_refresh.py"
            if required:
                log.error(msg)
                raise
            log.warning(msg)
            return False
        except Exception as e:
            log.error(f"Failed to load token: {e}")
            if required:
                raise
            return False

    def reload_token(self) -> bool:
        return self._load_token(required=False)

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TokenException as e:
            log.warning(f"TokenException ({e}) — reloading token and retrying once")
            if not self._load_token(required=False):
                raise
            return fn(*args, **kwargs)

    def is_token_valid(self) -> bool:
        try:
            self.kite.profile()
            return True
        except TokenException:
            return False
        except Exception as e:
            log.error(f"is_token_valid: unexpected error {e}")
            return False

    # ─── Orders ──────────────────────────────────────────────────────
    def place_order(self, tradingsymbol: str, transaction_type: str, quantity: int,
                    product: str = "NRML", exchange: str = "NFO",
                    ref_price: Optional[float] = None, tick: float = 0.05) -> str:
        """MARKET (with market_protection) or marketable LIMIT around ref_price.
        LIMIT falls back to MARKET when no usable ref_price is available."""
        otype = ORDER_TYPE
        kw = dict(
            variety=self.kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=int(quantity),
            product=product,
            validity=self.kite.VALIDITY_DAY,
            tag=ORDER_TAG,
        )
        if ALGO_ID:
            kw["algo_id"] = ALGO_ID
        if otype == "LIMIT" and ref_price and ref_price > 0:
            buy = transaction_type == "BUY"
            raw = ref_price * (1 + LIMIT_BUFFER_PCT / 100.0) if buy \
                else ref_price * (1 - LIMIT_BUFFER_PCT / 100.0)
            kw["order_type"] = self.kite.ORDER_TYPE_LIMIT
            kw["price"] = round_to_tick(raw, tick, up=buy)
            desc = f"LMT {kw['price']} (ref {ref_price} ±{LIMIT_BUFFER_PCT}%)"
        else:
            kw["order_type"] = self.kite.ORDER_TYPE_MARKET
            mp = MARKET_PROTECTION if MARKET_PROTECTION != 0 else -1
            kw["market_protection"] = int(mp) if float(mp).is_integer() else mp
            desc = f"MKT (protection {kw['market_protection']})"
        log.info(f"📤 ORDER {transaction_type} {quantity} {tradingsymbol} {desc} ({product}/{exchange})")
        oid = self._call(self.kite.place_order, **kw)
        log.info(f"   order_id = {oid}")
        return str(oid)

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._call(self.kite.cancel_order, variety=self.kite.VARIETY_REGULAR, order_id=order_id)
            return True
        except Exception as e:
            log.warning(f"cancel {order_id} failed (likely already terminal): {e}")
            return False

    def _last_state(self, order_id: str) -> Optional[dict]:
        hist = self._call(self.kite.order_history, order_id) or []
        return hist[-1] if hist else None

    def await_order(self, order_id: str, timeout: float = 8.0) -> dict:
        """Wait for a terminal state. Returns
           {status: COMPLETE|REJECTED|CANCELLED|PARTIAL|UNKNOWN,
            filled_qty: int, avg_price: float, message: str}
        An order still open at the deadline is CANCELLED and the partial
        fill (possibly 0) is reported as status PARTIAL."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self._last_state(order_id)
                st = (last or {}).get("status", "")
                if st in ("COMPLETE", "REJECTED", "CANCELLED"):
                    return self._result(st, last)
            except Exception as e:
                log.error(f"order_history({order_id}) error: {e}")
            time.sleep(0.4)
        log.warning(f"   order {order_id} not terminal after {timeout}s — cancelling remainder")
        self.cancel_order(order_id)
        for _ in range(10):
            try:
                last = self._last_state(order_id)
                st = (last or {}).get("status", "")
                if st in ("COMPLETE", "REJECTED", "CANCELLED"):
                    r = self._result(st, last)
                    if st == "CANCELLED" and r["filled_qty"] > 0:
                        r["status"] = "PARTIAL"
                    return r
            except Exception as e:
                log.error(f"order_history({order_id}) error after cancel: {e}")
            time.sleep(0.4)
        return self._result("UNKNOWN", last or {})

    @staticmethod
    def _result(status: str, o: dict) -> dict:
        r = {
            "status":     status,
            "filled_qty": int(float(o.get("filled_quantity", 0) or 0)),
            "avg_price":  float(o.get("average_price", 0) or 0),
            "message":    o.get("status_message") or "",
        }
        lvl = log.info if status == "COMPLETE" else log.warning
        lvl(f"   order → {status} filled={r['filled_qty']} avg={r['avg_price']} {r['message']}")
        return r

    # ─── Positions / margins ─────────────────────────────────────────
    def positions(self) -> dict:
        return self._call(self.kite.positions)

    def nfo_positions(self, product: Optional[str] = None) -> Optional[Dict[str, dict]]:
        """{tradingsymbol: {qty, avg_price, product, token}} for NFO net
        positions with non-zero quantity. None on API failure (callers must
        treat None as 'unknown', never as 'flat')."""
        try:
            raw = self.positions() or {}
        except Exception as e:
            log.error(f"positions() failed: {e}")
            return None
        out = {}
        for p in raw.get("net", []) or []:
            if p.get("exchange") != "NFO":
                continue
            if product and p.get("product") != product:
                continue
            q = int(float(p.get("quantity", 0) or 0))
            if q == 0:
                continue
            out[p["tradingsymbol"]] = {
                "qty":       q,
                "avg_price": float(p.get("average_price", 0) or 0),
                "product":   p.get("product"),
                "token":     p.get("instrument_token"),
            }
        return out

    def margins(self, segment: str = "equity") -> dict:
        return self._call(self.kite.margins, segment=segment)

    # ─── Market data ─────────────────────────────────────────────────
    def ltp(self, keys: List[str]) -> dict:
        return self._call(self.kite.ltp, list(keys))

    def bulk_ltp(self, keys: List[str]) -> Dict[str, float]:
        """{key: last_price} for 'EXCHANGE:TRADINGSYMBOL' keys. {} on failure."""
        keys = [k for k in dict.fromkeys(keys) if k]
        if not keys:
            return {}
        try:
            raw = self.ltp(keys)
        except Exception as e:
            log.error(f"bulk_ltp failed: {e}")
            return {}
        out = {}
        for k, v in (raw or {}).items():
            try:
                px = float((v or {}).get("last_price", 0))
                if px > 0:
                    out[str(k)] = px
            except (TypeError, ValueError):
                continue
        return out

    def instruments(self, exchange: str = "NFO") -> list:
        return self._call(self.kite.instruments, exchange)
