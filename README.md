# Autonomous Trading Agent — v1

> Autonomous trading agent app. The user connects a broker — **Alpaca**
> (US, paper + live) or **Zerodha Kite Connect** (India: NSE/BSE equity,
> NFO futures + options, intraday via MIS) — an LLM **council** drafts
> proposals; deterministic Python **disposes** (risk-checks, sizes,
> executes); the mobile app surfaces approvals and journals every decision.

**Status:** Phase 4 — paper-trading rollout. Council runs daily, the
operator hand-grades closed trades, calibration agreement signal is
live, LLM cost ledger is wired. Real Alpaca paper-trade smoke proven
end-to-end. **Deployable to Railway in 10 minutes — see [`RAILWAY.md`](RAILWAY.md).**

**Branch:** `agent-v1` (orphan — no shared history with previous TradeMatrix work).

---

## What works today

| Layer | What's there |
|---|---|
| **Mobile** (Expo + NativeWind) | Magic-link auth, biometric unlock, Alpaca OAuth connect, push notifications, Approvals inbox, Strategies tab, Review swipe-deck, Settings tab, Home health strip + agreement strip. |
| **API** (FastAPI) | 7 routers: auth, broker, notifications, account, activity, approvals, agent, orders, health, strategies, review. 30+ endpoints. HS256 JWT + scrypt-hashed refresh rotation + Fernet-encrypted broker tokens. |
| **Agent council** | 7/7 specialist nodes — Router, Technical, Fundamental, Macro, Selector, Drafter, Reflection. LangGraph + asyncio fallback. Mock + real Anthropic both wired. |
| **Risk engine** | 14 rules, market-aware (US + India) — drawdown halt, forbid-short, PDT (US), wash-sale (US), F&O lot-size, derivative-notional cap, MIS square-off window (IN), position size, sector, correlation cluster, etc. First-veto-wins ordering. |
| **Paper mode** | `TRADING_MODE=paper` (default) — simulated fills against per-market paper books (US $100K / IN ₹10L), full risk chain, zero broker calls. Two-key flip to live: `TRADING_MODE=live` + `LIVE_TRADING_ENABLED=1`. |
| **Brokers** | `BrokerInterface` + two adapters: `AlpacaBroker` (alpaca-py, OAuth or env keys) and `ZerodhaBroker` (Kite Connect v3 over httpx — equities, F&O via `NFO:` symbols, CNC/MIS/NRML products, daily-token auth). |
| **Executor** | `/orders/execute/{proposal_id}` — decrypt-on-use → live-trading gate (`LIVE_TRADING_ENABLED`) → re-evaluate risk → `place_order` on whichever broker is connected. Idempotent via `client_order_id` (native at Alpaca, tag-emulated at Zerodha). |
| **Persistence** | All 7 stores have Protocol + InMemory + Postgres impls (auth, broker, notifications, decision-log, strategy-confidence, review, cost-ledger). `USE_POSTGRES=1` env flips them all. |
| **Observability** | `/api/v1/health/full` reports per-component status. LLM cost ledger writes every call. Soft-cap warning at `LLM_COST_WARN_USD`. |
| **Tests** | **210 passed + 8 skipped.** Skips gate on a real `ANTHROPIC_API_KEY` or `RUN_POSTGRES_TESTS=1`. |

---

## Deploy + run mobile against it (10 minutes)

**Start here →** [**`HANDOFF.md`**](HANDOFF.md) — the single doc you
follow end-to-end: Railway deploy, Alpaca + Anthropic credential
wiring, Expo Go session, post-deploy smoke checklist. Includes
troubleshooting for everything that can go wrong.

[`RAILWAY.md`](RAILWAY.md) is the deeper reference — extended
troubleshooting, daily-cron scheduling examples, Fly-machines alt.
Short version:

```bash
# 1. Push this repo to GitHub
git push origin agent-v1

# 2. Railway dashboard → New Project → Deploy from GitHub → pick repo
#    (Railway auto-detects railway.toml + apps/api/Dockerfile)

# 3. Add Postgres plugin → DATABASE_URL is set automatically

# 4. Set the required env vars (see RAILWAY.md §4):
#    ENV=production, USE_POSTGRES=1, DEV_AUTH_BYPASS=0,
#    JWT_SECRET=<generated>, BROKER_TOKEN_ENCRYPTION_KEY=<generated>,
#    CORS_ORIGINS=exp://exp.host,https://exp.host

# 5. Run the mobile against it:
echo "EXPO_PUBLIC_API_URL=https://your-app.railway.app" > apps/mobile/.env
pnpm install
pnpm --filter @app/mobile dev   # scan QR with Expo Go
```

What's running by default: the council in **MOCK mode** (canned LLM
responses), in-memory broker tokens, the daily cron is unscheduled.
Add `ANTHROPIC_API_KEY` to flip the council to real Claude; add
Alpaca OAuth creds to enable the Connect Alpaca button. Both are
optional for first-launch verification.

---

## Local dev

```bash
# JS side — Expo dev server for mobile + UI package
pnpm install
pnpm --filter @app/mobile dev

# Python side — FastAPI + agent council
uv sync                                # one-time, locks Python deps
make dev-api                           # auth-enforced (DEV_AUTH_BYPASS=0)
make dev-api-legacy                    # pre-mobile-auth fallback if you need it

# Local Postgres (optional — defaults to in-memory)
make infra-up
make migrate
USE_POSTGRES=1 make dev-api

# Run the daily council against the in-memory stores
PYTHONPATH=apps/agents:packages/engine:packages/broker \
    python apps/agents/scripts/daily_cron.py --watchlist NVDA,AAPL

# Run the Reflection one-shot
PYTHONPATH=apps/agents:packages/engine:packages/broker \
    python -m trading_agents.reflection_cli --since 24h
```

Test suite (always green on clean checkout):

```bash
PYTHONPATH=apps/api:apps/agents:packages/engine:packages/broker \
DEV_AUTH_BYPASS=1 \
pytest packages/engine/tests/ apps/agents/tests/ apps/api/tests/ packages/broker/tests/
# → 210 passed, 8 skipped
```

---

## Where to look first

| File | What it answers |
|---|---|
| [`RAILWAY.md`](RAILWAY.md) | **Deploy step-by-step** + mobile-against-Railway setup |
| [`PLAN.md`](PLAN.md) | What we're building + the phase-by-phase implementation order |
| [`DESIGN.md`](DESIGN.md) | Mobile design system — tokens, components, accessibility |
| [`AGENTV1.md`](AGENTV1.md) | Running session log — what's built, what's next, current playbook |
| [`CLAUDE.md`](CLAUDE.md) | Agent-collaboration guide. Read before coding. |
| [`apps/api/AUTH.md`](apps/api/AUTH.md) | Auth + broker OAuth + push + executor + review + cost-ledger flow docs |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operator runbook: smoke harness, daily review checklist, cost tuning |

---

## Repo layout

```
.
├── apps/
│   ├── mobile/          Expo. 6 tabs (Home, Approvals, Strategies, Review, Settings + auth flow).
│   ├── api/             FastAPI gateway. Dockerfile + start.sh for Railway. 30+ endpoints.
│   └── agents/          LangGraph council. 7 nodes + Reflection out-of-band. Daily cron.
│
├── packages/
│   ├── shared-types/    TS wire types shared mobile ↔ api
│   ├── ui/              RN primitives + design tokens. ConfidenceBar, StatusPill, SwipeDeck, PnLPill, etc.
│   ├── broker/          BrokerInterface protocol + AlpacaBroker + ZerodhaBroker (Kite Connect)
│   └── engine/          Risk engine (11 rules), sizing, backtester, reconciler, db ORM
│
├── infra/
│   ├── docker-compose.yml   Local Postgres + TimescaleDB + Redis
│   └── migrations/          Alembic — 7 migrations (0001 schema → 0007 llm_calls)
│
├── docs/
│   └── RUNBOOK.md       Operator runbook
│
├── scripts/
│   └── smoke_paper_trade.py    End-to-end smoke against an Alpaca paper account
│
├── apps/api/Dockerfile  Multi-stage Python 3.12 image
├── railway.toml         Railway deploy config
├── .env.example         Every env var the deploy needs
└── Makefile             dev-api / dev-api-legacy / infra-up / migrate / test
```

**Architectural rule (the only one that matters):** Agents propose,
deterministic code disposes. The LLM council drafts narratives + picks
strategies + sizes proposals, but every order routes through
`packages/engine/risk` before reaching `packages/broker`. Risk vetoes
are pure Python with named `veto_rule` strings — never LLM output.

---

## What this is NOT

- **Not TradeMatrix.** The scoring engine is out of scope.
- **Not multi-market.** US equities + ETFs only in v1. India/Zerodha v2+.
- **Not options/F&O.** v3+.
- **Not live trading yet.** `is_paper=True` flows through every layer.
  PLAN.md §11 gates live capital on Phase 4 paper-validation closing
  (5–6 months of paper with the founder + 2–3 trusted users).

---

## Recent rounds

  - **2026-05-30 — Review tooling + LLM cost ledger.** Review tab with
    swipe-deck UX; agreement strip on Home; cost ledger writes every
    call; `/health/full` LLM cost pill lights up.
  - **2026-05-30 — Phase 4 kickoff.** Daily council cron; per-strategy
    P&L view; Strategies tab; Home health strip.
  - **2026-05-27 — Order executor + Alpaca paper-trade smoke.**
    `/orders/execute/{id}` route. Smoke harness gated on
    `RUN_ALPACA_SMOKE=1`.
  - **2026-05-27 — Postgres adapters.** All 5 stores get Postgres impls.
  - **2026-05-27 — Push notifications.** Expo Push + council fan-out hook.
  - **2026-05-26 — Alpaca OAuth + encrypted token storage.** PKCE flow
    + Fernet broker-token encryption.
  - **2026-05-26 — Mobile auth + biometric + deep-link.**
  - **2026-05-26 — Phase 3 kickoff (magic-link + JWT).**

See [`AGENTV1.md`](AGENTV1.md) for the full session log.
