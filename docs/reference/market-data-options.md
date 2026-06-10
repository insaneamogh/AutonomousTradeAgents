# Market Data Options — NSE/BSE/NFO (India) + US

**Research date: 2026-06-10.** Pricing and terms verified against current web sources where possible
(several official pages — kite.trade, zerodha.com, truedata.in — block automated fetchers, so some
figures come from secondary coverage of the official announcements and are flagged where approximate).
Re-verify on the vendor page before paying.

**Question being answered:** "I searched a lot of brokers but no one offers any complete data. Do I
use Angel Broking API to get this data?" — short answer: **no, you don't need Angel One anymore.**
The economics changed in Feb–Mar 2025: Zerodha now sells the complete data suite (live WebSocket +
quotes + ~10 years of historical intraday candles) for ₹500/month, and order/portfolio APIs are free.
Details below.

---

## 1. The headline change: Kite Connect pricing (2025 revamp)

Two announcements in early 2025 restructured Kite Connect:

1. **Feb 8, 2025 — historical data became a bundled feature, not an add-on.** The old
   "₹2,000/month Connect + ₹2,000/month historical add-on" model is gone. Historical candle API is
   auto-enabled on every paid Connect app.
2. **Mar 2025 — Kite Connect Personal launched (free), and the paid plan dropped to ₹500/month.**
   - **Kite Connect Personal (₹0):** order placement, positions, holdings, funds, postbacks — i.e.
     the full *trading* API — but **no market data at all** (no quotes, no WebSocket ticks, no
     historical candles). Built for people who bring their own data source.
   - **Kite Connect paid (₹500/month per API key):** everything in Personal **plus** live market
     data (quote APIs + WebSocket streaming) **plus** the historical candle API.

Sources: [Zerodha Z-Connect announcement](https://zerodha.com/z-connect/updates/free-personal-apis-from-kite-connect),
[Kite forum: fees revised ₹2000 → ₹500](https://kite.trade/forum/discussion/15015/revising-kite-connect-fees-from-2000-to-500-per-month),
[Kite forum: historical data now free with base subscription](https://kite.trade/forum/discussion/14806/historical-data-is-now-free-with-base-kite-connect-subscription),
[Marketcalls coverage](https://www.marketcalls.in/fintech/zerodha-makes-trading-api-free-for-personal-use-bundles-historical-data-with-connect-api.html),
[Zerodha support: API charges](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/what-are-the-charges-for-kite-apis).

### What the ₹500/month Kite Connect data suite actually gives you

| Capability | Detail |
|---|---|
| Quote API | Full quote (depth, OI, OHLC) for up to 500 instruments per call; ~1 req/s limit on `/quote` |
| WebSocket streaming | Up to **3 concurrent connections per API key**, up to **3,000 instruments per connection** (≈9,000 instruments total); LTP / quote / full-depth modes; binary protocol, very low overhead |
| Historical candles | minute, 3/5/10/15/30/60-minute, and daily intervals; **intraday history back ~10 years** (minute data from ~2015) for NSE/BSE/NFO/MCX; OI available for derivatives |
| Historical API rate limit | ~3 req/s (older docs) / 120 req/min observed — fine for a nightly watchlist sync, slow for bulk-universe backfills (plan an initial multi-hour backfill, then incremental) |
| Segments | NSE + BSE equity, NFO (futures & options), CDS, MCX — i.e. everything the owner trades via Zerodha |
| Order APIs | ~10 req/s, plus postback webhooks for fills |

Sources: [Kite Connect historical docs](https://kite.trade/docs/connect/v3/historical/),
[WebSocket docs](https://kite.trade/docs/connect/v3/websocket/),
[forum: 3 connections / 3,000 instruments](https://kite.trade/forum/discussion/15708/websocket-limit-behavior-subscribing-3000-instruments-per-connection-will-ticks-be-dropped),
[forum: rate limits](https://kite.trade/forum/discussion/14656/api-rate-limit).

**Verdict: yes, the owner can get everything from Zerodha alone.** Quotes, streaming, daily +
intraday history, equities + F&O, from the same account that executes — one auth flow, one
instrument-token namespace, zero symbol-mapping bugs between data and execution. The historic
reason people bolted Angel One onto Zerodha was the old ₹4,000/month price tag. At ₹500/month
that reason is gone.

Caveats that remain real:
- Daily access-token refresh (manual login or TOTP automation) — same as every Indian broker API
  post-SEBI rules.
- ₹500/month is per API key; one key is enough for one autonomous agent.
- No tick-level *historical* data (candles only). If you ever need historical tick/order-book
  data for research, that's vendor territory (TrueData et al.).
- SEBI's static-IP requirement for API trading (effective Apr 1, 2026, per broker notices) applies
  to execution regardless of which data source you pick.

---

## 2. Angel One SmartAPI — the direct answer

**What it is:** free API from Angel One (requires opening a free Angel One demat account).
Historically the default answer to "Zerodha data is too expensive."

| Aspect | Detail |
|---|---|
| Cost | ₹0 — trading, historical, and live data APIs are all free |
| Historical | `getCandleData`: 1-min → daily candles, equity + F&O + commodity; rate limit ~3 req/s / 180 req/min; intraday depth is shallower and patchier than Kite's (~a few years for 1-min, varies by segment) |
| Streaming | WebSocket 2.0; **3 connections per client code, ~1,000 tokens per connection** (vs Kite's 3×3,000); 20-depth feed in beta |
| Reliability | Mixed community reputation: forum threads report random 403/"access denied" and rate-limit errors well below documented limits, occasional candle gaps, instrument-master CSV churn, and login/TOTP breakage. Workable for hobby algos; needs defensive retry code for production |

Sources: [SmartAPI docs](https://smartapi.angelbroking.com/docs),
[SmartAPI forum: rate-limit changes](https://smartapi.angelone.in/smartapi/forum/topic/4387/changes-in-api-rate-limit),
[WebSocket 2.0 guide](https://smartapi.angelone.in/smartapi/forum/topic/3987/explore-the-smart-api-websocket-2-0-user-guide-depth-20-beta-testing),
[Chittorgarh review](https://www.chittorgarh.com/broker/angel-broking/api-for-algo-trading-review/14/).

**Is "Angel One for data + Zerodha for execution" sound?** It *works* — thousands of retail algo
traders ran exactly this split between 2021–2024 — but it is now a **₹500/month saving bought with
real engineering cost**:
- Two instrument-token universes (Angel `symboltoken` vs Zerodha `instrument_token`) that must be
  mapped and re-mapped on every corporate action / F&O expiry roll. Mapping bugs here are
  *silent wrong-data* bugs — the worst kind for an autonomous agent.
- Two daily login/TOTP flows to keep alive; two failure domains during market hours.
- Price ticks from Angel One can disagree with the Zerodha quotes your orders fill against
  (different feed handlers); slippage modeling and reconciliation get noisier.
- Angel One's documented reliability issues land in your *signal path*.

For an autonomous agent where decisions are audited and money is real, paying ₹500/month to
collapse all of that into one vendor is the right trade. **Recommendation: no, don't use Angel
One — use Kite Connect paid.** Keep SmartAPI in mind only as a free *fallback/secondary* feed
(see Recommendation section).

---

## 3. Comparison table — all options

| Source | Cost (verify before buying) | Historical candles | Live streaming | Rate limits | ToS / production risk |
|---|---|---|---|---|---|
| **Zerodha Kite Connect (paid)** | ₹500/mo per API key (orders free via Personal) | 1-min → daily, ~10 yrs intraday, NSE/BSE/NFO/MCX, OI | WebSocket, 3 conn × 3,000 instruments | quote ~1/s; historical ~3/s (120/min); orders ~10/s | Official, exchange-approved. Lowest risk. Same vendor as execution |
| **Angel One SmartAPI** | Free (needs Angel demat account) | 1-min → daily, shallower depth, occasional gaps | WebSocket, 3 conn × ~1,000 tokens | candle 3/s · 180/min; flaky enforcement reported | Official but reliability complaints; second account + token mapping to maintain |
| **Upstox (Uplink)** | Free | 1-min → daily, decent depth | WebSocket V3 (protobuf), solid | published per-endpoint limits | Official, free; currently *pays* ₹10/API order (promo to Mar 2026). Best free alternative to Kite |
| **Dhan (DhanHQ v2)** | Trading API free; **Data APIs ₹499/mo** | Yes, good F&O support, 20-depth feed | WebSocket incl. market depth | published, reasonable | Official; data costs the same as Kite — no reason to switch if already on Zerodha |
| **Fyers API** | Free | 1-min OHLCV, ~1–2 yrs intraday depth | WebSocket, good reputation | generous | Official; good free option but needs a Fyers account |
| **ICICI Breeze** | Free (ICICIdirect customers) | 3 yrs incl. second-level LTP | Streaming OHLC + ticks | moderate | Official; full-service-broker account overhead; clunkier dev experience |
| **5paisa** | Free–low cost (Xtra/API plans) | Basic candles | WebSocket | moderate | Official; thinnest docs/community of the broker set |
| **TrueData** | Custom quote; order of magnitude ₹1.5k–4k+/mo per segment (real-time API); historical extra | Deep, incl. tick & 1-min history, options analytics | Low-latency WebSocket, no broker login | by plan/symbols | Authorized NSE/BSE/MCX vendor. Overkill + overpriced for one personal agent |
| **Global Datafeeds** | ~₹2k+/mo (quote-based) | Yes (NimbleData APIs) | Yes, low latency | by plan | Authorized vendor; suits charting/HFT-ish users, multi-terminal setups |
| **Accelpix** | Budget vendor, roughly ₹1–2k/mo (site quote-gated) | Yes incl. tick history | Yes (Python/Amibroker/Ninja) | by plan | Authorized vendor; cheapest of the vendor trio; still > Kite ₹500 |
| **yfinance (.NS/.BO)** | Free | Daily OK-ish; intraday limited (60d of 15-min etc.) | None (delayed 15–20 min) | Aggressive 429s since 2025; IP bans | **Unofficial scraper.** Breaks without notice; NSE symbols randomly "delisted"; violates Yahoo ToS for automated use. Research-only, never in the signal path |
| **nseindia.com endpoints** | Free | Bhavcopy EOD is fine (official file) | No | Akamai cookie/UA blocking, datacenter IPs blocked | Unofficial (except bhavcopy downloads). Scrapers break weekly. Not production-worthy |
| — US — | | | | | |
| **Alpaca free (IEX feed)** | $0 | Full daily/minute history (IEX-sourced; SIP-sourced historical >15-min delayed available free) | WebSocket, IEX-only ticks (~2% of US volume) | 200 req/min | Official. Fine for swing trading on daily bars; quotes can deviate from NBBO intraday |
| **Alpaca Algo Trader Plus (SIP)** | $99/mo | Full SIP history + OPRA options | Full consolidated SIP WebSocket | 10,000 req/min | Official. Needed only if/when intraday US strategies ship |
| **Polygon.io** | Free tier 5 req/min; paid from ~$29; serious use ~$199/mo | Excellent depth incl. ticks | Strong WebSocket infra | by plan | Official vendor; great but redundant with Alpaca for this stack |
| **Tiingo** | Free tier; paid ~$10–30/mo ("Power") | 30+ yrs EOD, IEX intraday | IEX WebSocket | by plan | Official; superb cheap EOD/fundamentals for research |

Key sources: [Upstox trading API](https://upstox.com/trading-api/),
[Dhan API comparison](https://stratzy.in/blog/cost-comparison-algo-trading-apis-india/),
[ICICI Breeze FAQ](https://www.icicidirect.com/faqs/fno/what-is-the-cost-or-fees-for-using-breeze-api),
[TrueData](https://www.truedata.in/price), [Global Datafeeds](https://globaldatafeeds.in/),
[Accelpix](https://accelpix.com/), [yfinance rate-limit issue](https://github.com/ranaroussi/yfinance/issues/2422),
[yfinance NSE issue](https://github.com/ranaroussi/yfinance/issues/2612),
[Alpaca market data docs](https://docs.alpaca.markets/us/docs/about-market-data-api),
[Alpaca data plans](https://alpaca.markets/data), [Polygon pricing](https://polygon.io/pricing),
[Tiingo pricing](https://www.tiingo.com/pricing).

---

## 4. Current state of the repo (data-accuracy baseline)

Inspected 2026-06-10 on branch `agent-v1`. **Every market-data input in the codebase today is
synthetic or mock — there is no real market data anywhere, for either US or India.**

| Component | File(s) | Data source today |
|---|---|---|
| Agent council features | `apps/agents/trading_agents/features/synthetic.py`, wired as the default `feature_provider` in `apps/agents/trading_agents/runtime.py` | **Synthetic.** `synthetic_features()` hashes the ticker string into a deterministic 0–1 seed and fabricates price, ATR, RSI, DMAs, fundamentals (incl. a fake Piotroski score), and macro values (VIX, 10-yr yield, DXY). Same ticker → same fake numbers every run. `universe` is hard-coded `"US"` |
| Daily council cron | `apps/agents/scripts/daily_cron.py` | Runs the council over a static **US** watchlist (SPY, QQQ, AAPL, …) using the synthetic feature provider above; LLM itself can also run in MOCK mode |
| Risk officer context | `apps/agents/trading_agents/nodes/risk_officer.py` → `packages/engine/engine/risk/context.py` | **Mock by default** (`MockRiskContextProvider`). A `postgres_context.py` provider exists for real portfolio state, but that's account state, not market data |
| Backtester | `packages/engine/engine/backtester/feed.py`, `__main__.py` | `InMemoryBarFeed` fed by `_synthetic_bars()` (500 days of generated "AAPL-shaped" daily bars) for smoke runs; `CsvBarFeed` can read user-supplied CSV OHLCV — **no downloader/ingest exists to produce those CSVs** |
| Broker package | `packages/broker/broker/alpaca.py` | **Trading only** (orders, positions, account). No Alpaca data client (`StockHistoricalDataClient` / data WebSocket) is used anywhere |
| India anything | — | **Nothing.** No kiteconnect, smartapi, or NSE code exists in the repo; India is a v2 item per `CLAUDE.md` |

Implication: backtest results, agent confidences, and the cron's "decisions" currently have **zero
relationship to real markets**. The first real-data milestone (per Phase 1) is a bar-ingest that
writes real OHLCV into TimescaleDB/CSV for the backtester and a real feature provider replacing
`synthetic_features` — for US that's Alpaca's free data API; for India (when in scope) it's the
recommendation below.

---

## 5. RECOMMENDATION

### Optimal setup (personal use, already on Zerodha, wants intraday + F&O + equities)

**Single vendor: Zerodha Kite Connect paid plan — ₹500/month (~$6).**

- One API key gives quotes, WebSocket streaming (3×3,000 instruments — far beyond a swing/intraday
  watchlist), and ~10 years of 1-minute → daily history across NSE, BSE, NFO, and MCX.
- Data and execution share one account, one auth/session, one instrument-token namespace — the
  single biggest reliability and auditability win for an autonomous agent. The price you see is the
  price you trade.
- It is official and exchange-authorized: no ToS gray zone, no scraper breakage.
- Architecture fit: a small `data-ingest` worker holds the WebSocket + nightly historical sync into
  TimescaleDB; agents consume pre-computed feature dicts from that store (consistent with the
  "agents never originate raw data fetches" rule in `CLAUDE.md`).
- Build the ingest behind the existing `BarFeed` protocol so the backtester consumes Kite candles
  through the same interface as CSV/synthetic today.

**Answer to the owner's question directly:** you do **not** need Angel Broking. The "no broker
offers complete data" observation was true of the pre-2025 market; since Feb–Mar 2025 Zerodha
itself offers the complete set for ₹500/month. The Angel-One-for-data pattern is a legacy
workaround for Kite's old ₹4,000/month pricing.

### Runner-up

**Upstox Uplink (free) for data + Kite Connect Personal (free) for execution — total ₹0/month.**
Choose this only if ₹500/month genuinely matters. Upstox's data APIs are free, officially
supported, with a solid protobuf WebSocket (V3) and decent historical depth — a more reliable free
feed than Angel One by current community sentiment. Cost: you re-introduce the two-broker problems
(second account, dual token mapping, dual session keep-alive, feed-vs-fill price divergence). Use
Angel One SmartAPI only as a *third* choice / redundancy feed — its data is free and broad, but the
documented reliability flakiness makes it a poor primary for an autonomous agent.

**Do not** pay TrueData/Global Datafeeds/Accelpix for this use case (they suit multi-user charting
platforms, tick-history research, or sub-100 ms latency needs), and **never** put yfinance or
nseindia.com scraping in the live signal path — research notebooks only.

### US side (brief)

- **Now (Phase 0–1, daily bars, paper trading):** Alpaca's **free IEX-based data** is sufficient
  and is the obvious first real-data integration — the `alpaca-py` dependency is already in
  `packages/broker`. Daily bars and 15-min-delayed SIP historical data are reliable enough for
  1–10-day swing decisions.
- **Later (live intraday US, if ever):** upgrade to **Algo Trader Plus ($99/mo)** for consolidated
  SIP real-time data, or bolt on **Tiingo (~$10–30/mo)** for cheap deep EOD history and
  fundamentals for research. Polygon is excellent but redundant given Alpaca is already the broker.

---

*Prices checked 2026-06-10 via web search; official Zerodha/TrueData pages blocked automated
fetching, so re-confirm ₹500/mo on [kite.trade](https://kite.trade/) and vendor quotes before
subscribing.*
