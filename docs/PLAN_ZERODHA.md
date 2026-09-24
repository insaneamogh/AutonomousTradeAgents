# Plan: Zerodha (Kite Connect) as a first-class broker, not just Alpaca

> **Status: PROPOSED 2026-09-25. Not approved.** Written at the operator's
> request ("add a plan to integrate Zerodha as well, not just Alpaca").
> It follows `docs/PLAN_PLATFORM.md`: nothing trades in India either until
> a signal clears the same Phase 3 acceptance bar on Indian data.
> Every external fact below is quoted from a source checked on 2026-09-25;
> re-check before relying on one, because SEBI and NSE change these often.

## 1. What already exists (verified by reading the code, not the docs)

| Piece | Where | State |
|---|---|---|
| Kite Connect v3 broker | `packages/broker/broker/zerodha.py` | Orders, cancel, positions (holdings + net), equity/buying power from margins, tag-based idempotency (Kite has no client_order_id dedupe). **Now sends `market_protection`** (97dd854d2). |
| Connect flow | `apps/api/app/services/broker/zerodha_connect.py`, `routers/broker.py` | Login URL with CSRF state, request_token exchange, encrypted access token, stored daily expiry. |
| Daily token expiry | `broker_use.py` | Fails fast with "reconnect Zerodha" instead of a raw Kite 403. |
| Reconnect reminder push | `notifications.py` (`zerodha_reconnect`) | Exists; whether anything schedules it is **unverified**. |
| India risk rules | `engine/risk/markets.py`, `rules/lot_size.py`, `mis_square_off.py`, `derivative_notional.py` | Market detection from `EXCHANGE:SYMBOL`; US-only rules (PDT, wash sale) skip IN. |
| Paper engine | `apps/api/app/services/orders/paper_broker.py` | Simulated fills per (user, market), INR book. **In-memory: resets on restart.** Zerodha has no sandbox, so this is the only paper path India has. |
| Portfolio | `portfolio_service.py` | Reads Zerodha holdings. |

## 2. What is hardwired to Alpaca (the real gap)

Every background loop picks the broker by name, so a Zerodha user gets
orders placed and then nothing watching them:

| File | What it does with `"alpaca"` |
|---|---|
| `reconciler_fleet.py` | Lists only `alpaca` connections; polls with `broker="alpaca"`. No snapshots, no breaker, for Zerodha. |
| `order_sync.py` | `with_broker_client(user_id, broker="alpaca")`. Zerodha fills never heal decisions; closes never detected. |
| `position_manager.py` | Stops, trails, time stops, expiry sweep: Alpaca only. |
| `option_stops.py` | Resting stop-limit GTC. Kite regular orders are DAY/IOC only (our broker refuses GTC), so this design does not port. |
| `auto_approver.py`, `stale_entries.py` | Alpaca only. |
| `engine/features/*`, `bars.py` | All market data is Alpaca (US). The council cannot analyse an NSE symbol at all. |
| `scheduler.py`, `is_us_market_open` | US session only (14:00 UTC sweep is 19:30 IST, after NSE closes at 15:30). |

## 3. External constraints (quoted, with sources)

1. **Static IP is mandatory for API orders since 2026-04-01.** SEBI's retail
   algo framework: "A static IP is mandatory for everyone" placing orders
   via API; one primary plus one backup IP; and "within one broker — the
   same IP cannot be shared across unrelated users" (Zerodha, *SEBI algo
   trading changes, April 2026*). Orders from an unregistered IP are
   rejected.
   **Railway does not satisfy this as-is.** Its Static Outbound IPs (Pro
   plan) assign *three* load-balanced IPv4s with "no guarantee that the
   IPv4 addresses assigned to your service are dedicated" (Railway docs).
   Three shared addresses cannot be registered as one primary plus one
   backup, dedicated. **Needed: a dedicated egress** (a small VPS or a
   dedicated-IP proxy) that only Kite order traffic goes through.
2. **Market orders need market protection.** Kite: `market_protection`
   ">0 and up to 100 (custom %), or -1 (auto protection)", for MARKET and
   SL-M only. **Done** in 97dd854d2 (`KITE_MARKET_PROTECTION`, default -1).
3. **Rate limits** (Kite exceptions doc): quote 1 req/s, historical 3 req/s,
   orders 10/s, 400/min, 5000/day per key, 25 modifications per order.
   Above 10 orders/s a strategy must be registered with the exchange; this
   desk is nowhere near that and should stay under it by construction.
4. **Daily login, no refresh token.** Access tokens flush around 06:00 IST.
   The operator logs in every trading day. **Do not automate the login**
   (credential/TOTP scraping): it is the operator's action, and the app
   already has the push to prompt it. An unattended India desk therefore
   runs only on days the operator has logged in, and must degrade to
   "watch only" on the others.
5. **Data cost.** Kite Connect is ₹500/month per API key including live and
   historical data (Zerodha, Feb 2025 onward). The free "personal" API
   places orders but has **no market data**, so it cannot feed the council.
6. **Protective exits.** Regular orders are DAY/IOC. GTT (good till
   triggered) persists across sessions, supports single and two-leg (OCO)
   triggers, and takes **LIMIT** orders only; Kite's docs show CNC and do
   not document F&O/NRML support. So the US design (resting GTC stop-limit)
   becomes: GTT OCO for CNC equity; for F&O, a same-day SL order refreshed
   each session plus the software stop. Verify GTT on NRML before relying
   on it.
7. **F&O structure (2026).** Weekly expiries only for NIFTY (NSE) and
   SENSEX (BSE); others monthly. NIFTY lot size 65 from January 2026.
   Options STT on premium raised to 0.15%, futures to 0.05% (per brokerage
   summaries; **confirm against the NSE circular**). Lot sizes and freeze
   quantities change by circular, so **read them from Kite's daily
   instruments dump, never hardcode them.**

## 4. Phases

### Z0: Compliance and infrastructure (blocks any live Zerodha order)
- Dedicated static egress for Kite **order** endpoints only (VPS with
  WireGuard, or a dedicated-IP proxy). Register it (plus a backup) in the
  Kite developer console. Route `ZerodhaBroker` order calls through it with
  an explicit `KITE_ORDER_PROXY_URL`; everything else stays direct.
- Client-side order limiter at 8/s and 300/min (below Kite's 10/s, 400/min).
- ✅ `market_protection` (97dd854d2).
- Alert (ops_alerts) on a Kite 403 "IP not whitelisted" distinctly from an
  expired token, because the fixes differ.

### Z1: Make the loops broker-agnostic
- One helper: `connections_for_loops()` returning every active connection
  with its broker; every loop in §2 iterates connections, not the literal
  `"alpaca"`. `with_broker_client(user_id, broker=conn.broker)`.
- Per-market clock: `is_market_open(market, now)` with an NSE calendar
  (`exchange_calendars` XBOM, or NSE's holiday list; pick one and pin a
  test to a known holiday). The fleet's market-hours exit gate uses the
  position's market, not the US session.
- Zerodha has no account-activities feed like Alpaca's OPEXP/OPEXC. Option
  expiry in India is cash-settled for index options and physically settled
  for stock options (verify current NSE rule). order_sync infers the event
  from the instrument's expiry date plus the position vanishing, and
  labels `option_expired` / `option_settled` accordingly.
- A skipped Zerodha user (expired token) is logged and alerted once per
  day, never retried every 30s.

### Z2: Indian market data (feeds the council; no LLM in it)
- `KiteDataProvider`: daily bars from the historical API (3 req/s), quotes
  (1 req/s, batched up to Kite's per-call instrument limit), and the
  instruments dump cached daily (lot size, tick size, expiry, strike,
  freeze quantity).
- Option chain = instruments dump + quotes. Kite does not return greeks;
  compute IV and delta with `engine.options.pricing` (already used for the
  US backtest). India VIX from the same quote API.
- Record an `iv_history` row per NSE underlying from day one (same table,
  `feed='kite'`), for the same reason as the US recorder: history cannot
  be bought back later for free.

### Z3: India risk, sizing and costs (deterministic, named rules)
- Size in lots from the instruments dump; `lot_size_block` already exists
  and must read the dump, not a constant.
- Full Indian cost model in the risk engine and the backtester: brokerage,
  STT, exchange transaction charges, SEBI fee, stamp duty, GST. An edgeless
  signal loses mostly to costs, and these are larger than US options costs.
- Pre-trade margin check through Kite's order-margins API as a
  deterministic gate (`insufficient_margin`), before the order is sent.
- Product choice (CNC / MIS / NRML) is a named decision on the proposal,
  and `mis_square_off` stays the guard for intraday.
- Persist the paper engine to Postgres before any India shadow run; an
  in-memory book that resets on redeploy cannot produce a track record.

### Z4: Research before trading (same bar as the US)
- Load NSE history through Z2 and run the same harness:
  `signal_backtest`, `option_backtest` (with Indian costs and lot sizes),
  `acceptance.evaluate`, independent observations, net of costs.
- Nothing trades in India until a candidate passes. Until then India runs
  shadow: decisions recorded and graded by the forecast scorecard.

### Z5: Scheduling and operations
- IST scan times (a first sweep after the opening range, e.g. 10:00 IST),
  the IV snapshot and EOD report per market in INR, and the reconnect push
  at about 08:30 IST on NSE trading days.
- Kill switch and daily report per broker. Leader election already covers
  both markets (one process runs every loop).

## 5. Decisions that are the operator's (not filled in here)
1. Instrument: NSE cash equity (CNC), NIFTY/SENSEX options, or both.
2. Spend: ₹500/month Kite Connect plus a dedicated static IP
   (roughly ₹1,500/year for the IP alone per Zerodha's estimate, more for a
   managed proxy).
3. Where the egress lives: a VPS you control, or a paid dedicated-IP proxy.
4. Accepting that the India desk only acts on days you log in.
5. Order of work versus the US research track (PLAN_PLATFORM Phase 3), which
   still has no signal that passes.

## Sources
- Zerodha, "SEBI algo trading changes — April 2026" (inthemoneybyzerodha.substack.com)
- Kite Connect v3 docs: orders (`market_protection`, `autoslice`), exceptions (rate limits), GTT (kite.trade/docs/connect/v3)
- Zerodha support: Kite Connect pricing and historical data
- Railway docs: Static Outbound IPs (docs.railway.com/networking/static-outbound-ips)
- NSE lot-size and weekly-expiry summaries, January 2026 (brokerage summaries; confirm against NSE circulars)
