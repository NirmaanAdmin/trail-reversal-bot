# Zerodha Options Server — TradingView chassis → Kite Connect

TradingView Pine (fleet chassis, e.g. `ZScore_Breakout_NSE_Zerodha.pine` on an `NSE:` chart)
sends **entries** (and `close` on gate-block) to this Flask server. The server buys a long
**ITM option** on the underlying — `buy` → CE, `sell` → PE — carries it **NRML**, and
**owns every exit**: spot-based ladder SL, TP rungs, books, trailing, premium backstop,
combined P&L locks and the expiry guard.

Same webhook contract and safety model as the CoinDCX `server2.py`, so any chassis Pine
works unchanged apart from its symbol mapper.

---

## 1. Before anything trades — two hard requirements (Zerodha, since 1 Apr 2026)

| Requirement | Symptom if missing | Fix in this build |
|---|---|---|
| **Static IP registered on the Kite app** (SEBI retail-algo rules) | Every order rejected (e.g. "No IPs configured for this app") | `KITE_PROXY_URL` routes all Kite REST calls (and the daily login) through a fixed-IP proxy. Railway's outbound IP is dynamic, so on Railway this is mandatory. |
| **`market_protection` on API market orders** | "Market orders without market protection are not allowed via API" | `kiteconnect==5.2.2`. Default order type is a marketable **LIMIT** (LTP ± `LIMIT_BUFFER_PCT`); `ORDER_TYPE=MARKET` sends `market_protection=-1` (Kite auto). |

**Setup order:** set `KITE_PROXY_URL` → deploy → `GET /egress-ip?secret=…` → register that IP in
developers.kite.trade → your app → static IP → test with one small entry.

---

## 2. Webhook contract (POST /webhook) — identical to server2.py

```json
{"action":"buy","symbol":"HAVELLS","type":"entry","price":"1520.5","labeled":"1","secret":"…"}
{"action":"sell","symbol":"HAVELLS","type":"close","reason":"gate_block","price":"1519","secret":"…"}
```

* `leverage`, `margin_currency`, `tp_price`, `sl_price` are accepted and **ignored** (the server ladder is authoritative; options are cash-paid).
* `flat_only:"1"` + `setup_time` (epoch ms) → flat-only Rules A/B exactly as server2.
* `symbol` may be `HAVELLS`, `NSE:HAVELLS`, `BAJAJ_AUTO` (→ BAJAJ-AUTO), `M_M` (→ M&M), `NIFTY1!` (→ NIFTY), `CNXFINANCE` (→ FINNIFTY), `NIFTYJR` (→ NIFTYNXT50). Must be in `tradable_symbols.py`.

| Pine signal | Server state | Result |
|---|---|---|
| entry, `labeled:"0"` | any | **reject** (ghost / resync) |
| entry | flat | gates → position cap → size → **BUY CE/PE** |
| entry, same side | held | **skip** |
| entry, opposite side | held < `FLIP_FRESH_GRACE_SEC` | **skip** (anti-whipsaw; an option flip pays two spreads) |
| entry, opposite side | held | **FLIP**: sell CE/PE (fill-confirmed) → buy the other. If the sell doesn't fill, the flip aborts and the old leg stays managed. |
| close | held | sell everything (never gated) |
| close | flat | skip |

Entry gates: market hours 09:15–15:30 IST, daily cap, flatten cooldown, profit/loss-lock cooldowns, streak pause, per-symbol SL lockout. **Closes are never gated.**

---

## 3. Server-owned exits (`EXIT_POLL_SEC`, market hours)

On the **underlying spot** (mirrors the Pine ladder sim):
1. **Premium backstop** — option premium −`PREMIUM_SL_PCT`% from fill → close.
2. **Spot SL / trail** breached → close all lots, arm SL lockout.
3. **Rungs** every `TP_STEP_PCT`%: book `BOOK_PCT`% of original lots, up to `MAX_BOOKS`; SL ratchets (k=1 → breakeven, k≥2 → entry ± step·(k−1)), never loosens. A book never takes the last lots — **1–2 lot positions skip books and only trail.**

Plus: combined premium-P&L profit/loss locks (close all + cooldown), daily cap, streak pause, expiry guard (`EXPIRY_SAFETY_DAYS`, market hours only), optional daily flatten.

**Set `SL_PCT` / `TP_STEP_PCT` / `BOOK_PCT` / `MAX_BOOKS` to the same values as the Pine "Ladder mirror" inputs**, otherwise the chart's WR/MFE/Positive stats describe a different ladder from the one actually trading.

---

## 4. Safety invariants

* **Reduce-only sells.** Every SELL is clamped to the real long quantity in Kite positions. A stale tracker can never sell more than is held, so the bot cannot accidentally *write* (short) an option.
* **Fill-confirmed tracking.** State changes only on `order_history` results. A rejected BUY is never tracked; an order still open after `ORDER_CONFIRM_SEC` is cancelled and only its filled part is tracked.
* **Reconciler** (`RECONCILE_SEC`): tracked-but-gone → cleared; Kite holds less → tracker shrinks; Kite holds more → reported; **short on a bot option → bought back**; untracked long NRML options on whitelisted names → reported (or adopted with `RECONCILE_ADOPT=true`).
* Single gunicorn worker (required; state is in-process).

---

## 5. Deploy (Railway)

1. New repo/service from this folder. `Procfile` already pins `--workers 1 --threads 8`.
2. Variables from `.env.example` (required: `KITE_*`, `WEBHOOK_SECRET`, `KITE_PROXY_URL`).
3. Attach a volume at `/app/data` (trades, streak, last exits, token survive redeploys).
4. `GET /health` → `boot_done:true`. `GET /egress-ip?secret=…` → register the IP. `GET /status?secret=…`.
5. TradingView: add `ZScore_Breakout_NSE_Zerodha.pine` to an `NSE:<SYMBOL>` chart (seconds charts on NSE need TradingView real-time NSE data). Set **Webhook Secret**, keep your chassis inputs, then create the alert (*Any alert() function call*, webhook `https://<app>/webhook`). For a watchlist alert, every symbol on the list must be an F&O underlying.
6. After any Pine change: pause → replace → **delete & recreate all alerts** → unpause.

Token: refreshed headlessly at boot (if invalid) and daily 07:00 IST via TOTP, through the same proxy.

---

## 6. Endpoints (all need `?secret=` or `X-Auth-Secret`, except `/health`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | public liveness |
| `/webhook` | POST | Pine entry / close |
| `/status` | GET | trades (live spot & premium), locks, flat_only, flip_guard, reconcile, config |
| `/egress-ip` | GET | the IP Kite sees — register it |
| `/equity` | GET | available cash |
| `/reconcile` | GET/POST | run reconcile now |
| `/close-all` | POST | close every tracked position (no cooldown) |
| `/close-one?symbol=X` | POST | close one |
| `/clear-tracking?symbol=X` | POST | drop from tracking without trading |
| `/locks/clear` | POST | clear cooldowns, daily cap, streak, SL lockouts |
| `/daily-flatten/force` | POST | close all + flatten cooldown |
| `/refresh-instruments` | POST | reload NFO dump |
| `/underlyings` | GET | Kite F&O names vs whitelist |
| `/stats` | GET | last 100 events |

---

## 7. Files

```
server.py                         Flask app, gates, flip, ladder, locks, reconcile, endpoints
kite_client.py                    SDK wrapper: static-IP proxy, LIMIT/MARKET+protection, fill confirm, positions
token_refresh.py                  daily TOTP login (through the proxy)
instruments.py                    NFO dump cache (+ tradingsymbol index)
option_resolver.py                ITM strike / expiry picker (IST date)
position_sizer.py                 FIXED_PREMIUM_INR or EQUITY_PCT sizing, capped by cash
tradable_symbols.py               F&O whitelist + TradingView→Kite symbol folding
ZScore_Breakout_NSE_Zerodha.pine  the live Z-Score chassis with the NSE symbol mapper
fun2sh_options.pine               previous Pine (superseded, kept for reference)
```
