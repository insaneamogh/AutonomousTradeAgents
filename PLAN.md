# PLAN.md — Autonomous Agent Trading App

**Status:** Pre-build planning · v0.2 (US-first scope)
**Pivot from:** TradeMatrix (scoring platform)
**Author:** Amogh
**Last updated:** May 2026

---

## 1. The honest framing

You're not building "TradeMatrix on mobile." You're building something with a fundamentally different value proposition.

TradeMatrix sold information (scores). Information is cheap. Anyone with `yfinance` and a weekend can build a scoring engine. The market told you that.

This product sells **execution + judgment**. Users hand you their broker session and you place real money trades on their behalf. Much harder to build, much harder to earn trust for, much harder for a competitor to replicate. The moat is real, but so is the liability.

Three things have to be true for this to work:

1. **The agents have to actually make money** (or at minimum lose less than the user would on their own). 5–6 months of paper trading + your own real capital is the right call. Do not skip this.
2. **The execution layer has to be boringly reliable.** Order fails, partial fills, broker session timeouts, exchange halts. The agent layer can be exciting; the execution layer must be dull and bulletproof.
3. **You need regulatory clarity.** US is permissive for retail algo via Alpaca, but you still need clean disclosures and an audit trail.

---

## 2. v1 scope (locked)

- **Market:** US equities only
- **Broker:** Alpaca (paper first, then live)
- **Assets:** US equities + ETFs (no options/F&O in v1)
- **Cadence:** Swing first (1–10 day holds, daily bars), then intraday in v1.5
- **Target user:** Indian retail investors with US brokerage accounts (Vested/INDmoney/Groww-US adjacent TAM), plus US-based users
- **Positioning:** "Your US portfolio, on autopilot"

### Why this scope works

Alpaca gives you:

- Free paper trading API (instant signup, no daily re-auth)
- 30-day refresh tokens (vs Zerodha's daily login)
- Free market data (IEX) for v1, Polygon when revenue allows
- Permissive regulatory posture for third-party algo
- Single market hours (09:30–16:00 ET) — simpler agent scheduling
- No STT/LTCG complexity in risk engine
- Standard 1099 tax reporting (broker handles it)

### What v2 / v3 add

| Phase | Market | Assets | Timeline |
|---|---|---|---|
| v1   | US only | Equities + ETFs, swing | Months 1–8 |
| v1.5 | US | + Intraday equities | Months 8–10 |
| v2   | + India (Zerodha/Groww/Upstox) | Equities + ETFs | Months 10–15 |
| v3   | Both | + F&O / Options | Months 15+ |

---

## 3. Product overview

### What the user sees

1. Open app → biometric/PIN unlock
2. Connect Alpaca (one-time OAuth, 30-day refresh)
3. Three core surfaces:
   - **Portfolio + Psychology Report** — current holdings, P&L, behavioral analysis of past trades (loss aversion patterns, overtrading, revenge trading detection)
   - **Strategies** — browse pre-built, or build your own via natural language
   - **Live agent** — what the council is watching today, proposed trades, recent fills
4. Approve trades one-by-one, OR set auto-approval window (day/week)
5. End-of-day digest

### What the user pays for

Three plausible models, decision deferred to post-paper-trading:

| Model | Pro | Con |
|---|---|---|
| Flat subscription ($9–$29/mo) | Predictable revenue | Hard to justify to non-traders |
| Commission per executed trade | Aligned with usage | Encourages overtrading — bad UX, bad ethics |
| Performance fee (% above benchmark) | Strongest alignment | Triggers RIA registration in US |

**Recommendation:** Flat subscription with tiers based on (a) capital under management cap, (b) number of active strategies, (c) auto-approval enabled or not. Decide after paper trading data is in.

---

## 4. System architecture

```
┌──────────────────────────────────────────────────────────────┐
│   MOBILE (React Native, Expo + EAS)                          │
│   - OAuth flows, biometric, push notifications               │
│   - Approval UI, strategy builder, charts                    │
└──────────────────────────────────────────────────────────────┘
                          │  HTTPS + WSS
                          ▼
┌──────────────────────────────────────────────────────────────┐
│   API GATEWAY  (FastAPI on Fly.io)                           │
│   - Auth, rate limit, request validation                     │
└──────────────────────────────────────────────────────────────┘
                          │
       ┌──────────────────┼──────────────────┐
       ▼                  ▼                  ▼
┌────────────────┐  ┌────────────────┐  ┌─────────────────┐
│ AGENT COUNCIL  │  │ DETERMINISTIC  │  │ BROKER GATEWAY  │
│ (LangGraph)    │  │ ENGINE         │  │ - Alpaca v1     │
│ - Router       │  │ - Backtester   │  │ - (Zerodha v2)  │
│ - Analysts     │  │ - Risk engine  │  │ - (IBKR v3)     │
│ - Risk officer │  │ - Position     │  │                 │
│ - Executor     │  │   sizer        │  │ Abstraction     │
│ - Reflection   │  │ - Reconciler   │  │ layer common    │
└────────────────┘  └────────────────┘  └─────────────────┘
       │                  │                  │
       └──────────────────┼──────────────────┘
                          ▼
                ┌──────────────────────────┐
                │   DATA LAYER             │
                │   - Postgres (state)     │
                │   - TimescaleDB (OHLCV)  │
                │   - Redis (hot cache)    │
                │   - S3 (artifacts, logs) │
                └──────────────────────────┘
```

**Key principle: agents propose, deterministic code disposes.**

Every order an agent wants to place passes through a deterministic risk gate before it reaches the broker. Agents never call the broker API directly. This is the single most important architectural rule.

Even though v1 is Alpaca-only, build the broker abstraction layer from day one. The interface is cheap to design now and expensive to retrofit later when you add Zerodha/IBKR.

---

## 5. The agent council

Mixture-of-experts + model tiering.

### 5.1 Roles

| Agent | Responsibility | Model tier | Cadence |
|---|---|---|---|
| **Router** | Classify market regime, pick which analysts to invoke | Haiku 4.5 | Per tick / per session |
| **Technical Analyst** | Read charts, indicators, S/R levels | Haiku 4.5 or Sonnet 4.6 | On-demand |
| **Fundamental Analyst** | Earnings, news, sector context | Sonnet 4.6 | Daily / event-driven |
| **Macro Analyst** | Index/sector regime, rates, Fed flows | Sonnet 4.6 | Daily |
| **Strategy Selector** | Match current conditions to user's enabled strategies | Sonnet 4.6 | Per opportunity |
| **Risk Officer** | Veto power — checks against position limits, drawdown, correlation | Opus 4.7 + deterministic checks | Every proposed trade |
| **Executor** | Translates approved decision into broker-specific order params | Haiku 4.5 | Per order |
| **Reflection Agent** | Post-trade: was the rationale correct? Update strategy confidence | Sonnet 4.6 | EOD |

The tiering matches stated intent (cheap models for routine work, expensive models for judgment calls). Risk Officer gets Opus because it's the agent that prevents account-blowing mistakes — overspending there is correct.

### 5.2 Flow (LangGraph state machine)

```
[Market Tick / Schedule Trigger]
                ↓
[Router]                → classifies regime, picks analyst subset
                ↓
[Analyst N] runs in parallel  → produces structured signals
                ↓
[Strategy Selector]     → matches signals to user's active strategies
                ↓
[Proposal Drafter]      → creates concrete order proposal (ticker, side, qty, SL, target)
                ↓
[Risk Officer]          → deterministic checks + LLM judgment
                ├─ REJECT  → log, notify, end
                └─ APPROVE ↓
[User Approval Gate]
                ├─ Manual mode → push notif, wait for tap
                └─ Auto mode   → check auto-window validity, proceed
                ↓
[Executor]              → broker API call (idempotent, with retry)
                ↓
[Reconciler]            → confirms fill, updates Postgres
                ↓
[EOD Reflection Agent]  → updates strategy confidence scores
```

### 5.3 What agents do NOT do

- **No raw broker API calls.** Executor is a thin wrapper around a deterministic order-placement service.
- **No raw data fetching.** Analysts query a feature store; they don't hit `yfinance` / Alpaca data API directly.
- **No code execution in production.** Strategy code is generated, sandboxed-tested, then committed as a versioned artifact. The live agent picks from the artifact, doesn't write+run on the fly.
- **No memory of other users' trades.** Each user gets an isolated agent context. This matters for both privacy and to prevent correlated dumb behavior across the user base.

---

## 6. The deterministic engine (the boring, critical part)

This is where most of your engineering effort goes. It's also what separates you from "ChatGPT with broker access."

### 6.1 Backtester

Build **before** any agent. Without it you can't validate anything.

- Event-driven (not vectorized) — supports intraday, supports slippage models, supports realistic fill simulation
- Walk-forward by default — train on rolling N months, test on next M months
- Realistic costs — Alpaca is commission-free for equities, but model SEC fee + FINRA TAF + spread costs
- Slippage models — at minimum: fixed bps, volume-participation, spread-based
- Survivorship-bias-free universe — for US, use Polygon (has delisted tickers) or Norgate

Open-source starting point: `vectorbt` for prototyping, then graduate to custom event-driven engine. `backtrader` is a reference but its codebase is showing its age.

### 6.2 Risk engine

Pre-trade checks, all hard rules, none of them LLM-judgment:

- Position size ≤ X% of portfolio
- Sector concentration ≤ Y%
- Single-name concentration ≤ Z%
- Daily loss limit → halt agent
- Open positions ≤ N
- Buying power check (US: PDT rule for accounts < $25K — critical)
- Correlation cap (don't open 5 bank stocks because they all triggered the same signal)
- Wash sale tracking (US tax) — informational, not blocking

**PDT rule is non-negotiable for v1.** Accounts under $25K can only do 3 day trades per 5 business days. Risk engine must track this per account and block intraday closures that would trigger PDT.

### 6.3 Position sizer

Volatility-targeted by default (ATR-based or realized vol). Kelly fraction available as opt-in for advanced users. **Never percent-of-account fixed.**

### 6.4 Reconciler

Post-trade, every 30 seconds during market hours:

- Pull current positions from Alpaca
- Compare to expected positions in Postgres
- If mismatch → halt agent, alert user, log for forensic review

Catches: partial fills, manual user intervention from a different device, broker-side rejections that didn't propagate, network race conditions.

---

## 7. Mobile app (React Native)

### Stack

- React Native + Expo (managed workflow, EAS Build)
- Expo Router for navigation
- Zustand for state, TanStack Query for server state
- NativeWind for styling (Tailwind for RN)
- Reanimated 3 for chart interactions and approval flows
- Notifee for rich push notifications
- `react-native-keychain` for credential storage
- MMKV for encrypted local storage

### Screens (v1 scope)

1. **Onboarding** — sign up, Alpaca account verification
2. **Broker connect** — Alpaca OAuth handoff
3. **Home / Dashboard** — portfolio summary, today's agent activity, pending approvals
4. **Approval inbox** — swipe-to-approve/reject, with full agent rationale expandable
5. **Strategies** — marketplace + my strategies
6. **Strategy builder** — chat with agent to spec a strategy, see backtest before deploy
7. **Psychology report** — behavioral analysis
8. **Trade history** — with agent reasoning archived per trade
9. **Settings** — risk limits, auto-approval windows, notification preferences

### Auth UX (much simpler than Zerodha)

- One-time OAuth, 30-day refresh
- Biometric unlock on app open
- Push notification when refresh nearing expiry: "Reconnect Alpaca to keep agent active"
- No daily login dance

---

## 8. Data infrastructure

### Market data

- **v1:** Alpaca's free IEX feed (real-time consolidated tape comes with paid Alpaca)
- **When revenue allows:** Polygon.io ($79/mo starter — full SIP feed, options chains for v3)
- **Fundamentals:** Financial Modeling Prep API ($14–$50/mo), or Polygon's bundled fundamentals
- **News:** GDELT (free), Benzinga (paid when scale demands)

### Storage

- **Postgres** — user accounts, strategies, orders, agent decisions audit log
- **TimescaleDB** (Postgres extension) — OHLCV, tick data
- **Redis** — hot cache for live prices, agent state during a tick cycle
- **S3 / Cloudflare R2** — backtest artifacts, agent conversation logs (you'll need these for debugging and audit)

### Feature store

Even a small one (Feast or a custom thin wrapper over Postgres) — agents read pre-computed features (RSI-14, MACD, 50/200 MA, sector rank, etc.) rather than computing per call. Cuts agent latency 10x and cuts LLM costs by reducing tokens.

---

## 9. ML / pretrained models worth evaluating

Specific candidates, not just a list:

| Model | What it does | Where to use it |
|---|---|---|
| **FinBERT / FinGPT** | Financial sentiment from news/earnings | Fundamental Analyst input feature |
| **Chronos (Amazon)** | Zero-shot time series forecasting | Baseline price forecast as a feature, **NOT** a trade signal |
| **Lag-Llama** | Open-source time-series foundation model | Same as above |
| **Moirai (Salesforce)** | Time-series foundation model, multi-horizon | Multi-horizon volatility forecast input |
| **PatchTST / N-BEATS** | Train your own on your universe | When you have data + time |

**Critical caveat:** none of these alone produce alpha. They produce features that go into your decision logic. Don't let an agent see "Chronos predicts +2% tomorrow" and translate that into a buy — guaranteed way to lose money. Predictions are features, not signals.

---

## 10. Regulatory reality check (US v1)

Much simpler than India, but not free.

### Alpaca-specific:

- You operate on top of broker-approved API access — Alpaca handles broker-dealer obligations
- You're a software vendor, not a broker
- You never custody funds — Alpaca holds them

### Lines you must not cross without legal review:

1. **RIA registration** — if you charge for personalized investment advice, you're an Investment Advisor under the 1940 Act (or state-level if < $100M AUM). Self-approval per trade keeps you closer to "execution support." Auto-approval is murkier.
2. **Performance fees** — illegal for non-RIA, and even RIA needs qualified client thresholds.
3. **Disclosures** — risk disclosures, past-performance disclaimers, must be in-app and acknowledged.
4. **Audit trail** — every agent decision logged immutably with reasoning included.
5. **Marketing rules (2021 Marketing Rule)** — if you advertise backtest results, strict disclaimer requirements apply.

**Honest take:** Budget $2K–$5K for a fintech lawyer consultation in month 3. Specifically ask about RIA implications of auto-approval mode. Worth every dollar.

---

## 11. Implementation phases

### Phase 0 — Foundations (Weeks 1–4)

- Repo setup: monorepo (Turborepo) with `apps/mobile`, `apps/api`, `apps/agents`, `packages/shared-types`, `packages/ui`, `packages/broker`, `packages/engine`
- Postgres + TimescaleDB schema
- Alpaca paper integration — login, basic order placement, position fetch
- Broker abstraction layer (common interface — even though only Alpaca implements it in v1)
- Logging, observability (Better Stack or Axiom — generous free tiers)

### Phase 1 — Backtester + Risk engine (Weeks 5–8)

- Event-driven backtester
- Realistic cost modeling (US: SEC fee, FINRA TAF, spread)
- Risk engine with all pre-trade checks (including PDT rule tracking)
- Position sizer (vol-targeted)
- Reconciler
- 5 reference strategies hand-coded: SMA crossover, RSI mean reversion, momentum, breakout, volatility regime switch — your baselines

### Phase 2 — Agent council v0 (Weeks 9–14)

- LangGraph setup
- Router + 2 analysts (Technical, Fundamental)
- Risk Officer (deterministic-first, LLM-second)
- Strategy Selector
- Executor
- All running on paper trading
- End-of-day reflection agent

### Phase 3 — Mobile app v0 (Weeks 12–18, overlaps with Phase 2)

- Onboarding, Alpaca OAuth
- Approval inbox
- Portfolio view
- Strategy marketplace (start with the 5 reference strategies)
- Push notifications

### Phase 4 — Paper trading & internal capital (Weeks 18–28)

- You + 2–3 trusted users paper trade
- You connect real Alpaca with $200–$500 starting capital
- Weekly review of agent decisions
- Track every loss attribution: agent, data, execution, or strategy?
- Refine continuously

### Phase 5 — Beta launch (Weeks 28–36)

- Open to waitlist (50–100 users)
- Psychology report feature
- Strategy builder (NL → executable strategy)
- Pricing experiments

### Phase 6 — Public launch (Week 36+)

- Marketing
- Intraday strategies (v1.5)
- India phase planning (v2)

---

## 12. Additional features worth considering

Ranked by differentiation value:

| Feature | Value | Build cost | Priority |
|---|---|---|---|
| **Psychology / behavioral report** | High — genuinely unique | Medium | v1 |
| **Trade rationale archive** ("why did the agent buy NVDA on March 4?") | High — trust + learning | Low | v1 |
| **Strategy backtest before deploy** | High | Already building | v1 |
| **Tax loss harvesting agent** | High — measurable user benefit (US wash sale rules) | Medium | v1.5 |
| **Voice approval** ("Hey app, approve") | Medium | Medium | v2 |
| **Strategy social feed** — anonymized strategy performance from other users | Medium | High | v3 |
| **Paper-trade-first toggle** — every new strategy auto-paper-trades for 30 days before live | High — safety + trust | Low | v1 |
| **Drawdown circuit breaker** — agent auto-halts if account down >X% | High — safety | Low | **v1 (must-have)** |
| **Tax report export (1099 reconciliation helper)** | High at year-end | Medium | v1.5 |
| **Calendar-aware agent** — skip trading on user-marked travel days | Low | Low | v2 |
| **Sector/theme baskets** — "agent, run momentum on semiconductors only" | Medium | Low | v2 |
| **Earnings calendar integration** — auto-flatten before earnings on user's holdings | High | Low | v1.5 |

**Drawdown circuit breaker is non-negotiable.** Paper-trade-first toggle for new strategies protects users from themselves and protects you from liability.

---

## 13. Tech stack summary

| Layer | Choice | Why |
|---|---|---|
| Mobile | React Native + Expo | Single codebase, fast iteration |
| Backend API | FastAPI | Async, type hints, matches your Python comfort |
| Agents | LangGraph | State machines fit this problem |
| LLM routing | LiteLLM proxy | Swap Haiku/Sonnet/Opus per node, cost ledger |
| Data | Postgres + TimescaleDB + Redis | Boring, proven |
| Queue | River (Postgres) or Celery+Redis | River is simpler for solo dev |
| Deployment | Fly.io for API | Handles long-running well |
| Observability | Better Stack + Sentry | Both have generous free tiers |
| Secrets | Doppler | Don't `.env` this |
| CI/CD | GitHub Actions + EAS | Standard |

---

## 14. Open questions to resolve before Phase 0

1. **Co-founder / team structure** — solo? This is too big for solo in <12 months. If solo, what scopes down further?
2. **Capital** — agent infra + lawyer + data feeds will run $3K–$5K over 6 months even being frugal. Bootstrapped or seeking angel?
3. **Deloitte/BofA compliance** — this is a side project. Verify (a) employment contract permits it, (b) doesn't touch any internal Deloitte/BofA systems or data, (c) your trading list doesn't overlap with restricted lists from the BofA engagement. Talk to Deloitte ethics if uncertain.
4. **Brand / company structure** — register a Pvt Ltd in India before taking any revenue. For US users, may eventually need a US entity (Delaware C-corp typical). Lawyer in month 4–5.
5. **Liability insurance** — E&O insurance for fintech. Not optional once you go live with real money.

---

## 15. What to do this week

1. Decide: solo or co-founder
2. Spin up the monorepo, push to GitHub — **done**
3. Sign up for Alpaca paper account (instant, free)
4. Build the broker abstraction layer (`packages/broker`) with one method working end-to-end: `place_order(broker, symbol, side, qty)` → returns order ID on Alpaca paper
5. Stand up Postgres + a minimal `orders` and `agent_decisions` schema
6. **Don't touch agents yet. Backtester first.**

When Phase 0 is done, lay out Phase 1 in detail.
