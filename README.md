# Autonomous Trade Agents

**An autonomous US equities and options trading system for Alpaca paper
accounts. LLM agents propose trades, deterministic Python risk rules decide,
and every refused trade is marked to market so the value of a refusal can be
measured in dollars.**

It began as our entry to the Alpaca AI Trading Agents Hackathon ("The
Refusal Ledger", ended 2026-09-04). Since then it has been turned into a
research-first platform. The pitch-era material is kept under
[Hackathon archive](#hackathon-archive).

---

## Current status (2026-09-24)

> **The desk is not trading, and it should not trade yet.**

- **No edge.** A 6-year backtest of the production signal (13,403 signals,
  58 symbols) finds no edge at any horizon: hit rate 49.3–50.9%, and every
  horizon fails the pre-registered acceptance bar. The option backtest shows
  the signal losing 5–8% of premium per trade, while a perfect-direction
  oracle keeps +37–43%. **Options are not what loses money. The signal is.**
  Two alternative price-only signals also fail.
- **Live state.** The drawdown breaker latched on 2026-09-08 (−3.01%), and
  the Anthropic key was removed on 2026-09-11. The desk has been 100% HOLD
  since then.
- **The plan.** [`docs/PLAN_PLATFORM.md`](docs/PLAN_PLATFORM.md), approved
  2026-09-23, is the work queue. **Shadow mode** until a candidate signal
  clears the backtest bar. The LLM moves from choosing direction to
  interpreting events plus a veto, and runs on z.ai GLM
  (`LLM_PROVIDER=glm`). Each thesis trades in the instrument that matches
  its horizon.
- **Known shortcomings.** [`docs/SHORTCOMINGS.md`](docs/SHORTCOMINGS.md)
  is the audited list, with file:line evidence. The most urgent entries are
  safety gaps, not strategy:
  - A live Alpaca URL bypasses the two-key live-trading gate.
  - The kill switch does not stop options auto-trading.
  - The drawdown breaker never re-arms after it is acknowledged.
  - A keyless deployment emits canned equity proposals unless
    `AGENTS_REQUIRE_REAL_LLM=1` is set.
  - Daily bars are not split-adjusted.

| Plan phase | State |
|---|---|
| 0: clean-up, cap revert | Done |
| 1: z.ai GLM cutover | Code done. The live check needs a GLM key (see [`docs/PROVIDERS.md`](docs/PROVIDERS.md)) |
| 2: "too defensive" bugs | Done |
| 3: research harness | Built. **No candidate passes** |
| 4: missing entry gates | Partly done: breakeven gate, earnings calendar, IV-history recorder. Regime gate deferred |
| 5 / 5b: instrument router, debit spreads, event interpreter | Not started. Waits on a signal with an edge |
| 6: unattended ops | Mostly done: alerts, close retries, market-hours exits, restart catch-up, kill switch endpoint |
| 7: go-live criteria | Defined, not met |

---

## The one architectural rule

**Agents propose, deterministic code disposes.**

- An LLM is never in a risk or execution decision. Every veto is a named
  Python rule (`drawdown_halt`, `pdt_block`, `max_premium_pct`,
  `expected_move_below_breakeven`, …) and the first veto wins.
- Agents never call the broker. Every order goes through
  `packages/engine/risk` → `packages/broker`.
- Agents receive pre-computed feature dicts. They never fetch raw data.
- LLM output is never `eval()`'d.

---

## How a trade flows

```
Scanner: named triggers on intraday bars          deterministic · free
   ↓ only symbols that fire (plus a daily baseline sweep)
strategy_fit: 5 strategies × long/short           deterministic · free · most die here
   ↓ only symbols with a setup
Equity council: Router → analysts → Drafter       LLM · rationed by day/hour/$ caps
Options council: Bull ⇄ Bear → resolve()          LLM · must agree; size = min conviction
   ↓ a proposal or a guarded tool call
Risk engine: named equity + options rules         deterministic · re-derived on every call
   ↓ approved                        ↘ refused → Refusal Ledger (priced on real quotes)
Human approval, or an unattended path (both off by default)
   ↓
Alpaca paper → 30s reconciler fleet → exits (ratchet, resting stop, DTE sweep, time stop)
```

**Unattended execution** has two separate switches, both off by default:

| | Equity (and legacy-path options) | Options (Bull/Bear council) |
|---|---|---|
| Switch | `AUTO_APPROVE_ENABLED=1` plus per-connection owner consent | `AUTO_TRADE_ENABLED=1` plus `USE_OPTIONS_AGENT=1` |
| Where | `apps/api/app/services/orders/auto_approver.py` | `apps/agents/trading_agents/options/tools/guard.py` |
| Live money | Refused; paper only | Refused; paper only |

Human-approved **live** trading exists by design. It needs a live broker
connection, `LIVE_TRADING_ENABLED=1` and that connection's own live-trading
consent (`services/orders/live_trading_gate.py`). `TRADING_MODE` does not
gate this path; the unattended paths refuse unless it is `paper`. The gate
has a known bypass: see
[`docs/SHORTCOMINGS.md` §2.1](docs/SHORTCOMINGS.md).

**Exits** on an open option, all deterministic:
- a proportional trailing ratchet (`engine/options/exits.py`);
- a broker-side resting stop-limit (`services/orders/option_stops.py`);
- a DTE≤2 expiry sweep;
- a time stop.

Exits submit only while the market is open. The resting stop covers
off-hours.

---

## What is built and worth keeping

- **Risk engine**: equity rules in `packages/engine/engine/risk/rules/`,
  options rules in `packages/engine/engine/options/rules/`. Each is a
  named, tested Python function.
- **Refusal Ledger and ghost P&L**: every refusal is priced against real
  Alpaca quotes, and the result is shown on the Insights screen.
- **Contract funnel**: a six-stage chain filter whose per-stage counts are
  persisted, so a HOLD explains itself.
- **Reconciler fleet**: converges broker state into Postgres every 30s.
  It covers fills, external closes, the drawdown breaker and account-switch
  detection.
- **Ops**: alerts (Sentry, push, optional `OPS_ALERT_WEBHOOK_URL`),
  unique close-retry ids, restart catch-up for the daily sweep, and
  `POST /api/v1/positions/flatten-all` (endpoint only, no UI button).
- **Research harness** (`apps/agents/tests/eval/`): runs offline with no
  keys.
  - `signal_backtest`: 6 years, 58 symbols.
  - `option_backtest`: Black-Scholes premium P&L, plus an `--oracle` mode.
  - `acceptance`: the pass/fail bar.
  - `candidates`, `forecast_scorecard`, `exit_replay`, `entry_quality`.
  - The `AlphaModel` seam (`packages/engine/engine/alpha/`) lets a new
    signal be measured without writing a new harness.
- **LLM layer** (`apps/agents/trading_agents/llm.py`): Anthropic, z.ai GLM
  or Jev via `LLM_PROVIDER`, with a cost ledger and hard caps (20
  symbols/day, 4/hour, $3.00/day by default).
- **Data**: Alpaca IEX bars, Alpaca option chains (15-minute-delayed
  indicative feed by default, `ALPACA_OPTIONS_FEED=opra` for real time),
  FRED (VIX, 10y, dollar index), Alpaca news, a Finnhub earnings calendar
  (`FINNHUB_API_KEY`), and a daily ATM-IV history recorder.

---

## Repository layout

| Path | What |
|---|---|
| `apps/api` | FastAPI + SQLAlchemy 2 async. Routers, the order executor, the reconciler fleet, the council scheduler, notifications |
| `apps/agents` | LangGraph councils, LLM providers, strategies, jobs (`daily_cron`, `iv_snapshot`), and the research harness in `tests/eval/` |
| `apps/mobile` | Expo / React Native app, with a separate desktop web tree in `src/desktop/` |
| `apps/mcp_server` | Our own read-only MCP server, exposing the council *to* MCP clients |
| `packages/engine` | Deterministic core: risk rules, options selection, pricing and exits, features, the reconciler, DB models |
| `packages/broker` | Broker abstraction. Alpaca (paper and live) and Zerodha (live only, not reconciled) |
| `packages/shared-types`, `packages/ui` | TS types and UI kit shared by the mobile app |
| `infra/` | `docker-compose.yml` (Postgres, Redis) and Alembic migrations |
| `docs/` | Plans, playbooks and the audit; see [Docs map](#docs-map) |

---

## Quick start

```bash
make install            # pnpm install + uv sync --all-packages
make infra-up           # local Postgres + Redis (docker compose)
make migrate            # Alembic upgrade head
make dev-api            # FastAPI on :8000 (auth required)
make dev-mobile         # Expo
```

Configuration lives in [`apps/api/.env.example`](apps/api/.env.example).
Everything that can place an order is **off** unless you set it:

| Variable | Default | Effect |
|---|---|---|
| `COUNCIL_SCHEDULER_ENABLED` | off | Arms the daily sweep, the trigger scanner and the IV snapshot |
| `ALLOW_OPTIONS` / `ALLOW_SHORTS` | off | Arms the options / short rule pipelines. Without them, every such order is refused |
| `USE_OPTIONS_AGENT` | off | Routes options symbols to the Bull/Bear council |
| `AUTO_TRADE_ENABLED` | off | Lets the options council place paper orders unattended |
| `AUTO_APPROVE_ENABLED` | off | Lets the reconciler auto-execute approved equity proposals (also needs per-connection consent) |
| `AGENTS_REQUIRE_REAL_LLM` / `AGENTS_REQUIRE_REAL_DATA` | off | **Set both in production.** Without them, a missing key silently degrades to mock output |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `glm` or `jev`. See [`docs/PROVIDERS.md`](docs/PROVIDERS.md) |
| `LIVE_TRADING_ENABLED` | off | Global key for real-money orders on a live connection (also needs per-connection consent) |
| `TRADING_MODE` | `paper` | The unattended paths refuse anything but `paper` |

Deploy: `railway up --service AutonomousTradeAgents --detach`. Migrations
run at boot.

---

## Verification

```bash
# Full Python suite: 1833 passed, 11 skipped at 262bba9e (2026-09-24)
.venv/bin/python -m pytest apps/agents apps/api packages/ -q

# The research harness: offline, no keys, fixtures in apps/agents/tests/eval/fixtures
cd apps/agents
../../.venv/bin/python -m tests.eval.run_eval                  # 100-case funnel scorecard
../../.venv/bin/python -m tests.eval.signal_backtest           # the "no edge" result
../../.venv/bin/python -m tests.eval.option_backtest --oracle  # premium P&L vs an oracle
../../.venv/bin/python -m tests.eval.candidates                # pre-registered candidates

# Lint / TypeScript
.venv/bin/python -m ruff check <paths>
pnpm -s exec tsc --noEmit -p apps/mobile/tsconfig.json
pnpm --filter mobile exec jest --silent
```

There is **no CI**. These run only when someone runs them.

---

## Docs map

| Doc | Read it for |
|---|---|
| [`docs/PLAN_PLATFORM.md`](docs/PLAN_PLATFORM.md) | **The work queue.** Why trades lose, the data plan, and phases 0–7 |
| [`docs/SHORTCOMINGS.md`](docs/SHORTCOMINGS.md) | **The audited gap list**, with file:line evidence and what was not verified |
| [`docs/OPTIONS_PLAYBOOK.md`](docs/OPTIONS_PLAYBOOK.md) | The authoritative options rule set; §5 lists traps that have already bitten |
| [`docs/PLAN_ENTRY_EDGE.md`](docs/PLAN_ENTRY_EDGE.md) | Entry quality and the breakeven gate |
| [`docs/PROVIDERS.md`](docs/PROVIDERS.md) | LLM providers, and the steps for the GLM cutover |
| [`docs/RAILWAY_CHECKS.md`](docs/RAILWAY_CHECKS.md) | Deployment checks, ranked by consequence |
| [`docs/README.md`](docs/README.md) | Module map and architecture diagrams. Partly hackathon-era; check against the code |
| [`fable5findings.md`](fable5findings.md) | The build log: what each session changed and verified |

### Hackathon archive

Accurate for 2026-09-04, not for today:
[`docs/HACKATHON.md`](docs/HACKATHON.md) ·
[`docs/ONE_PAGER.md`](docs/ONE_PAGER.md) ·
[`docs/SUBMISSION_FINDINGS.md`](docs/SUBMISSION_FINDINGS.md) ·
[`docs/VIDEO_SCRIPT.md`](docs/VIDEO_SCRIPT.md), plus the older `PLAN_*` and
`IMPL_*` docs. Where they disagree with `PLAN_PLATFORM.md`, the plan wins.

---

## For AI models working on this repo

1. [`CLAUDE.md`](CLAUDE.md): §0 is your commit identity trailer, §4 the
   engineering standard. Every rule there exists because a bug got past a
   green test suite.
2. [`docs/PLAN_PLATFORM.md`](docs/PLAN_PLATFORM.md), then
   [`docs/SHORTCOMINGS.md`](docs/SHORTCOMINGS.md).
3. The newest entries under `# Build log` in `fable5findings.md`, newest
   first.

Read the code before trusting a doc, this one included.

---

## Disclosure

Paper-trading results are hypothetical and do not represent actual trading.
Options trading carries substantial risk. Nothing in this repository is
investment advice.
