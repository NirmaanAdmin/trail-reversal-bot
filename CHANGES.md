# CHANGES — 2026-10-22 · server2 parity + Zerodha 2026 compliance

## Blockers fixed (orders were being rejected)
- `kiteconnect` 5.0.1 → **5.2.2**: `market_protection` on MARKET orders (mandatory via API since 1 Apr 2026). Default order type is now a marketable LIMIT (`ORDER_TYPE`, `LIMIT_BUFFER_PCT`).
- **Static-IP egress**: `KITE_PROXY_URL` routes all Kite REST calls and the TOTP login through a fixed-IP proxy; `/egress-ip` shows the IP to register.

## server2.py contract
- `type:"close"` handled (gate_block / kill_switch / sl_wait…), never gated.
- Labeled gate (`labeled:"0"` rejected), flat-only Rules A/B (`flat_only`, `setup_time`, persisted last-exit times).
- Fresh-leg flip guard (`FLIP_FRESH_GRACE_SEC`, default 15 s).
- Daily flatten (off by default, NRML carry), `/status` blocks: flat_only, flip_guard, reconcile, config.
- `/reconcile`, `/clear-tracking`, `/egress-ip`, `/daily-flatten/force`.

## Correctness fixes vs the previous options build
- **Rejected BUY was tracked as a filled position** (fill-wait returned None → LTP used as the fill). Now only confirmed fills are tracked; open remainders are cancelled.
- **SELL updated tracking on placement, not on fill**, and was unclamped: a stale tracker could sell more than held, i.e. write an option. Every sell is now reduce-only against Kite positions and updates by filled qty.
- Flip closed the old leg and reopened even if the close failed; now aborts and keeps the old leg managed.
- Expiry sweep ran outside market hours (rejected order every poll); now market hours only, with retry backoff.
- A book could sell the last lot(s); books now always leave a trailing remainder (Pine mirror parity).
- Option LTPs keyed by `NFO:tradingsymbol` in one bulk call with spots; lock workers reuse the ladder's price cache.
- Resolver used the container's UTC date; now IST.
- Webhook symbols folded from TradingView spelling (BAJAJ_AUTO, M_M, NIFTY1!, CNXFINANCE…).
- Sizer: `FIXED_PREMIUM_INR` option, never sizes beyond available cash.

## Verification
- `ast` + `pyflakes` clean on all modules.
- 32 scenario checks against a simulated Kite broker: entry/limit/tag/sizing, same-side skip, fresh-flip guard, labeled gate, flip CE→PE, close, flat-only A/B, books 1–2 + trail ratchet + SL exit + lockout, reduce-only clamp after a manual partial close, rejected BUY, flip abort on rejected SELL, reconcile vanished, short cover, unfilled-order cancel, index alias, profit lock + cooldown, whitelist, /status auth.
- Not tested against the live Kite API — first live test: one small entry after `/egress-ip` registration.
