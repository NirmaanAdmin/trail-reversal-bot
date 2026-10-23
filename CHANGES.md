# CHANGES — Security & Robustness Hardening

Applied to `server2.py` + `Procfile`. Surgical edits only — every existing
incident-hardened guard (INJ/ZEC/HYPE margin races, 2026-05-29 reverse-window
race, 1000SHIB phantom-prune grace, GIGGLE step-rounding, TAO loss-cooldown
phantom entry) is preserved unchanged.

## What changed

1. **Procfile pinned to one worker.**
   `--workers 1 --threads 8 --timeout 120 --preload-app false`. The in-memory
   state + 5 background threads require exactly one process; this makes that
   explicit and prevents a future `--workers N` from duplicating orders and
   splitting state. `--preload-app false` is required so daemon threads start
   per-worker (they don't survive fork under preload).

2. **Auth is now fail-CLOSED everywhere.** New `_secret_ok()` (constant-time via
   `hmac.compare_digest`) + `_request_secret()` (accepts `X-Auth-Secret` header
   OR `?secret=`). If `WEBHOOK_SECRET` is unset, every authed path now REJECTS
   instead of silently exposing destructive endpoints. All 11 admin endpoints +
   the webhook were converted from the old fail-open `if WEBHOOK_SECRET and ...`.

3. **Webhook secret no longer logged.** Payloads are logged through `_redact()`,
   which masks `secret` as `***`. (Previously the secret was written to Railway
   logs in cleartext on every webhook.)

4. **One trade lock (`threading.RLock`) serializes state mutation.** Wraps the
   webhook action dispatch and the mutating section of `close_all_positions`.
   Closes the request-thread vs worker-thread races that could throw a swallowed
   `KeyError` and leave a half-closed position. Cooldown-arming stays *above* the
   lock (must win the race); streak/daily-cap registration stays *below* it.

5. **Book branch closes with the trade's stored leverage/margin** (was using the
   webhook payload's, unlike reverse/close).

6. **Single FX-rate source.** New `usdt_inr_rate()` accessor; removed the
   duplicate inline `os.environ["USDT_INR_RATE"]` reads in `compute_net_roe` that
   let sizing and P&L run on different rates.

7. **Robust webhook body parse.** `request.get_json(force=True, silent=True)`
   with a clean fallback — tolerant of TradingView's `text/plain` content-type,
   no silent no-trade on a parse miss.

8. **`/status` now requires the secret; `/health` trimmed to counts.** `/status`
   exposed the full book including exact SL levels; it's gated. `/health` no
   longer returns the open-symbol list (counts only, still public for uptime).

## Required actions on deploy

- **Update `/status` monitoring/bookmarks** to send the secret (`?secret=...` or
  the `X-Auth-Secret` header). Without it `/status` returns 403.
- **Confirm `WEBHOOK_SECRET` is set** in Railway. If it's empty, the server now
  rejects ALL webhooks and admin calls by design (fail-closed). This is
  intentional — set it before going live.
- **Do not add `--workers`** above 1, and do not enable `--preload`.
- Prefer the `X-Auth-Secret` header over `?secret=` going forward (query strings
  land in access logs). The query param still works during migration.

## Deliberately NOT changed (recommended follow-ups, do under test)

- **Native SL is still disabled** (`place_native_sl` is a no-op). There is no
  exchange-side stop; all stops remain logical via Pine webhooks + the loss-lock
  worker. Re-enabling needs the correct CoinDCX stop order type first — do not
  ship blind (a wrong type fires an immediate market close, which is why it was
  disabled).
- **Merging profit-lock + loss-lock into one worker** (halves CoinDCX position
  polling, removes a same-instant double-fire race) — behavioral, verify on paper.
- **Short-TTL cache on `fetch_real_positions_map`** for read paths only — note
  `wait_for_symbol_flat` needs uncached truth, so this is subtle.
- **Splitting the 2,700-line module** into config/client/state/risk_gates/routes.

## Verification run before packaging

- `py_compile` clean on `server2.py` + `coindcx_client.py`.
- Module imports with no load-time errors.
- Flask test-client: webhook no/wrong secret → 401; valid → processed; malformed
  body → graceful 200; admin no secret → 401/403; admin with `?secret` and with
  `X-Auth-Secret` header → 200; `/status` no secret → 403; `/status` with secret
  → 200; `/health` → 200 with no symbol list.
