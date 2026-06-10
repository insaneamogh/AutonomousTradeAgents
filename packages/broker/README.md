# packages/broker

Broker abstraction. **Implemented: Alpaca (US equities) + Zerodha Kite Connect (Indian equities + F&O).** IBKR slots in later without touching anything else.

## Layout
Interface (`base.py`) + wire types (`types.py`) + Alpaca implementation (`alpaca.py`) + Zerodha implementation (`zerodha.py`) + CLI smoke test (`__main__.py`).

### Smoke test (Alpaca)
```bash
cp .env.example .env       # fill in ALPACA_API_KEY / ALPACA_API_SECRET (paper)
uv run python -m broker --smoke                # account read + 1-share BUY + cancel
uv run python -m broker --smoke --no-order     # account read only
uv run python -m broker --smoke --symbol AAPL --qty 1
```
The smoke test refuses to run against the live Alpaca endpoint — it checks `is_paper` and bails otherwise.

## The contract
`BrokerInterface` is a `Protocol`, not an ABC — duck-typing keeps mocks and adapters easy. Required methods:

- `place_order(request) → Order`
- `cancel_order(broker_order_id) → Order`
- `get_order(broker_order_id) → Order`
- `list_positions() → list[Position]`
- `get_position(symbol) → Position | None`
- `get_account_equity() → float`
- `get_buying_power() → float`

All async. All idempotent on `client_order_id` where the broker supports it.
Money values are in the **account's native currency** (USD for Alpaca, INR for Zerodha) — the risk engine works in ratios, so it doesn't care.

## Alpaca (`alpaca.py`)
- Wraps the sync `alpaca-py` SDK in `asyncio.to_thread`.
- Two auth paths: `api_key + secret_key` (env/smoke) or `oauth_token` (per-user, decrypted-on-use by the API).
- Paper vs live via the explicit `paper: bool` flag.
- Idempotency is native: Alpaca de-dupes on `client_order_id` for ~24h.

## Zerodha (`zerodha.py`)
Hand-rolled async client over `httpx` against Kite Connect v3 — no `kiteconnect` SDK dependency.

**Auth (read before touching — it is NOT OAuth):**
- App-level `api_key` + `api_secret` from your [Kite Connect app](https://developers.kite.trade).
- The user logs in at `kite.zerodha.com/connect/login?v=3&api_key=…` → Zerodha redirects to the app's **registered redirect URL** with a single-use `request_token`.
- `exchange_request_token()` swaps it (sha256 checksum) for a daily `access_token`.
- **Access tokens expire daily ~06:00 IST.** No refresh tokens — the user re-logs-in every trading day. `next_token_expiry()` computes the flush time so the API layer can say "reconnect Zerodha" instead of surfacing a Kite 403.

**Symbol convention:** `EXCHANGE:TRADINGSYMBOL` — `NSE:RELIANCE`, `NFO:NIFTY24DECFUT`, `NFO:NIFTY2461923500CE`. Bare symbols default to NSE. This matches Kite's quote-API convention, so symbols round-trip cleanly.

**Products (intraday / delivery / derivatives):**
- NSE/BSE equity defaults to `CNC` (delivery). Set `KITE_DEFAULT_PRODUCT=MIS` (or pass `default_product="MIS"`) for intraday.
- NFO/MCX/CDS derivatives default to `NRML` — futures + options can't be CNC.

**Order-type mapping:** `MARKET→MARKET`, `LIMIT→LIMIT`, `STOP→SL-M`, `STOP_LIMIT→SL`. TIF: `DAY`/`IOC` only — `GTC`/`FOK` raise loudly rather than silently degrade.

**Idempotency (emulated):** Kite has no client-order-id dedupe — the `tag` field is an annotation, not a key. `place_order` lists today's orderbook first and returns any live order carrying the same tag (REJECTED/CANCELLED don't count), giving retry semantics equivalent to Alpaca's within the trading day.

**Positions:** `list_positions()` merges demat holdings + net day positions per symbol — the risk engine wants combined exposure, not Kite's settled-vs-today split.

**Live only:** Kite has no paper environment. `is_paper` is always `False`; the API's executor refuses non-paper orders unless `LIVE_TRADING_ENABLED=1`.

### Env vars
| Var | Used by | Notes |
|---|---|---|
| `KITE_API_KEY` / `KITE_API_SECRET` | API connect flow + client construction | from developers.kite.trade |
| `KITE_ACCESS_TOKEN` | `ZerodhaBroker.from_env()` only | manual smoke/CLI use |
| `KITE_DEFAULT_PRODUCT` | order placement | `CNC` (default) / `MIS` / `NRML` |
| `KITE_API_BASE`, `KITE_LOGIN_BASE` | tests/staging | default `api.kite.trade` / `kite.zerodha.com` |

### Tests
```bash
PYTHONPATH=packages/broker pytest packages/broker/tests/ -v
```
All network is mocked via `httpx.MockTransport` — no Kite account needed.

## Rule
**Agents never call this.** Calls route through `packages/engine/risk` (PDT, drawdown, position-size, correlation checks) which then calls the broker.
