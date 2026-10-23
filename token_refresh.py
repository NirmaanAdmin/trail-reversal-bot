"""
token_refresh.py
────────────────
Daily Kite Connect access-token auto-refresh.

Kite access tokens expire every day at ~06:00 IST (SEBI requirement — no
broker escapes this). This module performs the headless login flow using
your user_id + password + TOTP secret, exchanges the request_token for
an access_token, and writes it to ACCESS_TOKEN_PATH for the main server
to consume.

The main server (server.py) calls this on startup if the cached token is
invalid, and once per day at 07:00 IST via a background thread.

Uses Zerodha's undocumented internal login endpoints — exactly what the
kite.zerodha.com web UI calls. They are stable but not part of the public
API contract. If Zerodha ever changes their login flow, this is the file
that breaks.
"""
import os
import re
import sys
import logging
import requests
import pyotp
from kiteconnect import KiteConnect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("kite.token_refresh")

API_KEY     = os.environ.get("KITE_API_KEY", "")
API_SECRET  = os.environ.get("KITE_API_SECRET", "")
USER_ID     = os.environ.get("KITE_USER_ID", "")
PASSWORD    = os.environ.get("KITE_PASSWORD", "")
TOTP_SECRET = os.environ.get("KITE_TOTP_SECRET", "")
TOKEN_PATH  = os.environ.get("ACCESS_TOKEN_PATH", "/app/data/kite_access_token.txt")


def _proxies():
    """Login through the same static-IP proxy as orders (consistent source IP
    avoids repeated new-IP CAPTCHA / security prompts on kite.zerodha.com)."""
    for k in ("KITE_PROXY_URL", "QUOTAGUARDSHIELD_URL", "QUOTAGUARDSTATIC_URL", "FIXIE_URL"):
        v = os.environ.get(k, "").strip()
        if v:
            return {"http": v, "https": v}
    return None


def _check_env():
    missing = [
        k for k, v in [
            ("KITE_API_KEY", API_KEY), ("KITE_API_SECRET", API_SECRET),
            ("KITE_USER_ID", USER_ID), ("KITE_PASSWORD", PASSWORD),
            ("KITE_TOTP_SECRET", TOTP_SECRET),
        ] if not v
    ]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


def _step1_password_login(sess: requests.Session) -> str:
    """POST credentials to kite.zerodha.com/api/login. Returns request_id."""
    r = sess.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": USER_ID, "password": PASSWORD},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Step 1 (password) failed: {r.status_code} {r.text}")
    payload = r.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Step 1 status not success: {payload}")
    request_id = payload["data"]["request_id"]
    log.info("Step 1 OK: password accepted, got request_id")
    return request_id


def _step2_totp(sess: requests.Session, request_id: str) -> None:
    """POST TOTP code. Mutates the session cookies on success."""
    code = pyotp.TOTP(TOTP_SECRET).now()
    r = sess.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": USER_ID,
            "request_id": request_id,
            "twofa_value": code,
            "twofa_type": "totp",
            "skip_session": "",
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Step 2 (TOTP) failed: {r.status_code} {r.text}")
    payload = r.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Step 2 status not success: {payload}")
    log.info("Step 2 OK: TOTP accepted")


def _step3_request_token(sess: requests.Session) -> str:
    """Hit /connect/login and extract request_token from the redirect URL.

    Kite redirects to the registered redirect_url (recommend `http://127.0.0.1`
    for headless setups) with ?request_token=...&action=login. The requests
    session can't follow that scheme, or returns 404 — in either case the
    redirect URL containing the token is in r.url or the exception message.
    """
    url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
    try:
        r = sess.get(url, allow_redirects=True, timeout=15)
        final = r.url
    except requests.exceptions.ConnectionError as e:
        final = str(e)
    except Exception as e:
        final = str(e)

    if "request_token=" not in final:
        raise RuntimeError(
            f"request_token not in redirect URL — got: {final[:300]}"
        )
    # Use regex to stop at the first non-alphanumeric char. Splitting on
    # `&`, `'`, `"` alone breaks when `final` is an exception string with
    # the URL embedded inside parens/spaces (the requests.ConnectionError
    # path) — the parser would grab e.g.
    #   `hiO34tli1idM5THYovRbMUlgvc3LChqG (Caused by NewConnectionError(`
    # which Kite then rejects as "Token is invalid or has expired."
    m = re.search(r'request_token=([A-Za-z0-9]+)', final)
    if not m:
        raise RuntimeError(f"request_token regex match failed — got: {final[:300]}")
    token = m.group(1)
    log.info(f"Step 3 OK: extracted request_token ({len(token)} chars)")
    return token


def refresh() -> str:
    """Full refresh flow. Returns the new access_token and writes it to TOKEN_PATH."""
    _check_env()
    sess = requests.Session()
    px = _proxies()
    if px:
        sess.proxies.update(px)
    request_id = _step1_password_login(sess)
    _step2_totp(sess, request_id)
    request_token = _step3_request_token(sess)

    kite = KiteConnect(api_key=API_KEY, proxies=px)
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]

    os.makedirs(os.path.dirname(TOKEN_PATH) or ".", exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(access_token)
    log.info(f"✅ Wrote access_token to {TOKEN_PATH} ({len(access_token)} chars)")
    return access_token


def main():
    try:
        refresh()
        sys.exit(0)
    except Exception as e:
        log.error(f"❌ Token refresh failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
