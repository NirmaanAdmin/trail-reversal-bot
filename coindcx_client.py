import json
import time
import hmac
import hashlib
import logging
import requests

log = logging.getLogger("bot.coindcx")
BASE_URL = "https://api.coindcx.com"

class CoinDCXFutures:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()

    def _sign_and_post(self, endpoint, body):
        body["timestamp"] = int(round(time.time() * 1000))
        json_body = json.dumps(body, separators=(",", ":"))
        sig = hmac.new(self.api_secret.encode(), json_body.encode(), hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-AUTH-APIKEY": self.api_key, "X-AUTH-SIGNATURE": sig}
        url = f"{BASE_URL}{endpoint}"
        try:
            resp = self.session.post(url, data=json_body, headers=headers, timeout=15)
            log.info(f"POST {endpoint} → {resp.status_code}")
            if resp.text:
                return resp.json() if resp.status_code == 200 else {"status": "error", "code": resp.status_code, "message": resp.text}
            return {"status": "error", "code": resp.status_code, "message": "empty response"}
        except Exception as e:
            log.error(f"Request failed: {e}")
            return {"status": "error", "message": str(e)}

    def place_order(self, pair, side, order_type, total_quantity, leverage=5, price=None, stop_price=None, margin_currency="INR", tp_price=None, sl_price=None):
        order_data = {"side": side, "pair": pair, "order_type": order_type, "total_quantity": total_quantity, "leverage": leverage, "margin_currency_short_name": margin_currency}
        if price is not None:
            order_data["price"] = price
        if stop_price is not None:
            order_data["stop_price"] = stop_price
        if tp_price is not None:
            order_data["take_profit_price"] = tp_price
        if sl_price is not None:
            order_data["stop_loss_price"] = sl_price
        body = {"order": order_data}
        log.info(f"📤 {order_type} {side} {total_quantity} {pair} @ {price or stop_price or 'MARKET'} ({leverage}x) TP={tp_price} SL={sl_price}")
        result = self._sign_and_post("/exchange/v1/derivatives/futures/orders/create", body)
        if isinstance(result, list) and len(result) > 0:
            return result[0]
        return result

    def set_tp_sl(self, pair, side, quantity, tp_price, sl_price, leverage=5, margin_currency="INR"):
        """Set TP/SL using dedicated create_tpsl endpoint in a background thread."""
        import threading
        def _set_tpsl_worker():
            position_id = self._wait_for_position(pair)
            if not position_id:
                log.error(f"❌ Cannot set TP/SL: position never appeared for {pair}")
                return
            body = {
                "id": position_id,
                "take_profit": {
                    "stop_price": str(tp_price),
                    "order_type": "take_profit_market"
                },
                "stop_loss": {
                    "stop_price": str(sl_price),
                    "order_type": "stop_market"
                }
            }
            log.info(f"📤 create_tpsl for {pair} (pos={position_id}) | TP={tp_price} SL={sl_price}")
            result = self._sign_and_post("/exchange/v1/derivatives/futures/positions/create_tpsl", body)
            if isinstance(result, dict) and result.get("status") == "error":
                log.error(f"❌ TP/SL failed: {result}")
            else:
                log.info(f"🎯 TP/SL set ✅: {result}")

        thread = threading.Thread(target=_set_tpsl_worker, daemon=True)
        thread.start()
        log.info(f"🔄 TP/SL background thread started for {pair}")
        return True, True  # optimistic return, thread handles actual placement

    def _wait_for_position(self, pair, max_wait=120, interval=5):
        """Poll for position to appear, up to max_wait seconds."""
        import time as _time
        elapsed = 0
        while elapsed < max_wait:
            _time.sleep(interval)
            elapsed += interval
            try:
                positions = self.get_positions()
                if isinstance(positions, list):
                    for p in positions:
                        if p.get("pair") == pair and abs(float(p.get("active_pos", 0))) > 0.0001:
                            log.info(f"📍 Found position {p.get('id')} for {pair} after {elapsed}s")
                            return p.get("id")
                log.info(f"⏳ Waiting for {pair} position... ({elapsed}/{max_wait}s)")
            except Exception as e:
                log.error(f"Position poll error: {e}")
        return None

    def get_positions(self):
        return self._sign_and_post("/exchange/v1/derivatives/futures/positions", {})

    def cancel_all_orders(self):
        return self._sign_and_post("/exchange/v1/derivatives/futures/cancel_all_orders", {})
