# Agent Collaboration Guide — Autonomous Trading App

**Read this fully before writing any code. Then read [`docs/HACKATHON.md`](docs/HACKATHON.md).
If you are touching anything options-related, also read
[`docs/OPTIONS_PLAYBOOK.md`](docs/OPTIONS_PLAYBOOK.md) — it is the authoritative
rule set, derived from the code, and §5 lists the traps that have already bitten.**

**Work queued for you, in priority order.** Each is a full implementation plan with
verified measurements, a revert-check matrix, and a "where you will go wrong" section.
Read the whole plan before starting it:

| # | Plan | What |
|---|---|---|
| — | **IMPLEMENTATION SPECS — build these, in this order** | |
| I1 | [`docs/IMPL_LLM_TOOLS.md`](docs/IMPL_LLM_TOOLS.md) | `llm.py` tool support. **Foundation — I2 depends on it.** ~4h |
| I2 | [`docs/IMPL_OPTIONS_AGENTS.md`](docs/IMPL_OPTIONS_AGENTS.md) | Bull/Bear agents + guarded `open_option_trade` / `adjust_option_position`. ~16h |
| I3 | [`docs/IMPL_CONTRACT_FUNNEL_UI.md`](docs/IMPL_CONTRACT_FUNNEL_UI.md) | The funnel view. **Highest demo value per hour**, no dependencies. ~5h |
| I4 | [`docs/IMPL_REFUSAL_LEDGER.md`](docs/IMPL_REFUSAL_LEDGER.md) | Make the ledger show real dollars. **Starts with a diagnose-before-you-fix step.** ~6h |
| I5 | [`docs/IMPL_DEMO_SESSION.md`](docs/IMPL_DEMO_SESSION.md) | Read-only judge link. ~4h |
| | | |
| 0 | [`docs/PLAN_NEXT.md`](docs/PLAN_NEXT.md) | **START HERE.** What is left after the four below shipped, in order, plus the product gaps found reviewing the live app. |
| 0a | [`docs/PLAN_AUTO_APPROVE.md`](docs/PLAN_AUTO_APPROVE.md) | **Do this first.** The agent cannot open a trade today — entries are human-gated. Nothing trades Mon–Thu without it. |
| 0b | [`docs/PLAN_LEDGER_SURFACE.md`](docs/PLAN_LEDGER_SURFACE.md) | The Refusal Ledger renders $0 across the board. It is the entry's whole differentiator. |
| 0c | [`docs/PLAN_JUDGE_SURFACE.md`](docs/PLAN_JUDGE_SURFACE.md) | Judges hit a login wall, cannot see WHY an options trade was picked, and nothing on screen proves we use Alpaca's CLI. |
| 0e | [`docs/PLAN_CLI_SURFACE.md`](docs/PLAN_CLI_SURFACE.md) | **Deliverable 1/3.** The Alpaca CLI works and is invisible — one System Health row makes it judge-visible. ~1 hour. |
| 0f | [`docs/PLAN_MCP_DEMO.md`](docs/PLAN_MCP_DEMO.md) | **Deliverable 2/3.** Alpaca's MCP server, read-only. The §0 verification gate is PASSED — spec fetched 2026-08-31. |
| 0g | [`docs/PLAN_OPTIONS_AGENTS.md`](docs/PLAN_OPTIONS_AGENTS.md) | **Deliverable 3/3, ~3 days.** Two arguing options agents with REAL trade tools (`open_option_trade` / `adjust_option_position`), a deterministic guard running the full risk stack inside every call, a trailing ratchet that only tightens, and the trail state fed back for scale-in/exit decisions. Plus `llm.py` tool support, SSE debate UI, charts. |
| 0d | [`docs/PLAN_MULTI_TENANT.md`](docs/PLAN_MULTI_TENANT.md) | **§1 is a live security issue.** Any judge who signs up is auto-attached to the operator's Alpaca account with write access. Fix before inviting anyone. |
| 1 | [`docs/PLAN_AGGRESSIVE_PROFILE.md`](docs/PLAN_AGGRESSIVE_PROFILE.md) | Loosen the caps for the contest window via a reviewed profile. Changes outcomes; the delta band is frozen after Monday's open. |
| 2 | [`docs/PLAN_EXIT_AGENT.md`](docs/PLAN_EXIT_AGENT.md) | Trailing ratchet (hold winners) + a monotone LLM exit agent with read-only tools. |
| 3 | [`docs/PLAN_ALPACA_MCP.md`](docs/PLAN_ALPACA_MCP.md) | **Eligibility requirement.** Starts with a blocking verification gate — no code until you have quoted the spec. |
| 4 | [`docs/PLAN_CANDLE_PATTERNS.md`](docs/PLAN_CANDLE_PATTERNS.md) | Candlestick detection feeding strategy fit + the chart. |

---

## 0. Who you are — identify yourself in every commit

Two different models work on this repo, on two different accounts, in alternating
sessions. The user hits a 5-hour limit on one and hands over to the other. Neither of
you can see the other's conversation. **The git log is the only channel between you.**

Every commit message MUST end with an identity trailer:

| If you are… | Use this trailer |
|---|---|
| Claude **Opus** (primary session) | `ID:MODEL1REAL` |
| Claude **Sonnet** (handover session) | `ID:MODEL2OFF` |
| Any other model | `ID:MODEL2OFF` |

Put it on its own line, last, after the `Co-Authored-By:` line:

```
fix(options): stop vetoing every options proposal

<body>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
ID:MODEL1REAL
```

**Do not guess.** If you are not certain which model you are, you are `ID:MODEL2OFF`.

### Before you start any session

```bash
git log --oneline -20                       # what happened while you were away
git log -1 --format=%B                      # full message of the last commit
sed -n '/^# Build log/,/^### /p' fable5findings.md | head -60
```

The last commit's body and the newest `fable5findings.md` entry are written *for you*.
They will tell you what is done, what is verified, and what is left open. Read them
instead of re-deriving context or asking the user to repeat themselves.

### Before you end a session

Leave the next model a landing pad. Append to `fable5findings.md` (see §6) covering:
what you changed, **what you actually verified vs. what you only believe**, and what is
still open. If you ran out of budget mid-task, say exactly where you stopped.

---

## 1. What we are doing right now

**Competing in the Alpaca AI Trading Agents Hackathon. Deadline: Fri Sep 4, 11:00 AM
EDT.** Full brief, requirements, competitive landscape, and day plan:
**[`docs/HACKATHON.md`](docs/HACKATHON.md) — read it before touching anything.**

The 20-second version:

- Our entry is **"The Refusal Ledger"** — we are the only team measuring, in dollars,
  what the agent's *refusals* were worth. Ghost P&L is the product, not a side feature.
- **Options trading is a hard requirement.** So is using **Alpaca's own MCP server or
  CLI** (see §2 — this one has already been got wrong once).
- Judged on: P&L Performance · Technology Implementation · Creativity · Presentation.
- ~4 trading sessions of runway. P&L is variance-dominated over that window; the
  Refusal Ledger is what makes the entry strong regardless of which way P&L lands.

---

## 2. The MCP requirement — read this twice

The hackathon rule is: *"projects must utilize either **Alpaca's** MCP server or its
CLI tools."*

`apps/mcp_server/` exposes **our council TO Claude**. That is the opposite direction.
It is a well-built, well-tested, read-only server and it is genuinely nice to have —
but **it does not satisfy the requirement**, and shipping only that risks eligibility.

- **Keep `apps/mcp_server/`.** It is a bonus ("our agent is itself MCP-addressable").
  Do not delete it.
- **Additionally** consume Alpaca's own tooling:
  - MCP server: `uvx alpaca-mcp-server` (65 tools, `ALPACA_TOOLSETS` scoping,
    `place_option_order`, `get_option_chain`, `get_option_snapshot` with Greeks)
  - CLI: `github.com/alpacahq/cli` — explicitly built for "long-running agent sessions,
    cron jobs and CI", which is exactly `apps/api/app/services/council/scheduler.py`

**The lesson, generalised — this is the rule that would have prevented it:** when a
requirement comes from an external spec, *open the spec and quote it* before building.
Do not build against a plausible reading of a requirement you have not read. Cheap to
check, expensive to get wrong.

---

## 3. The one architectural rule

**Agents propose, deterministic code disposes.**

- Agents never call broker APIs directly. Every order routes through
  `packages/engine/risk` → `packages/broker`.
- Agents never originate raw data fetches. They receive pre-computed feature dicts.
- LLM output is **never** the kill-switch. Risk vetoes are deterministic Python with
  named rules (`pdt_block`, `max_premium_pct`, `illiquid_contract`, …).
- Never `eval()` LLM output in the live path.

If a feature request would put LLM output inside a risk decision or execution path,
push back.

**Note for the hackathon:** this framing alone is now commoditised — at least five
competitors claim it. It is still how we build; it is no longer what makes us
distinctive. The Refusal Ledger is.

---

## 4. How to work here — the standard this repo is held to

This codebase has repeatedly shipped bugs that a passing test suite did not catch. Every
rule below exists because something real got through.

### 4.1 A test that passes before your fix proves nothing

**Always revert your fix and confirm the new test fails.** Then restore it.

Three separate bugs shipped behind green tests here:
- The options capstone test had a `HOLD` escape hatch that `return`ed before its
  assertions ran. 100% of options proposals were being vetoed in production while it
  stayed green.
- Two executor fixtures set `symbol` to the OCC string, making `symbol` and
  `occ_symbol` indistinguishable — so reading the wrong field still produced the
  right value.

If you cannot make your test fail by breaking the code, you have not written a test.

### 4.2 Do not trust docstrings, comments, or plan docs

Verified wrong in this repo, all found by checking:
- `MinimalOptionsContextProvider`'s docstring says to compute `days_to_earnings` from
  corporate actions. `features/corporate_actions.py` states plainly that Alpaca
  publishes **no earnings calendar**. The docstring is wrong.
- `CLAUDE.md` itself claimed LiteLLM routing that does not exist anywhere in the code.
- `docs/OPTIONS_PLAN.md` says "Status: proposal, not built" — most of it shipped.
- `strategy_confidence.py` describes an LLM Selector node that was deleted.

Read the code. When a doc and the code disagree, the code wins — then fix the doc.

### 4.3 Measure against reality, don't reason about it

The liquidity gate was rejecting 89% of valid option contracts. No amount of reading
found it; one call to the live chain did:

```
volume>=10 via last_trade_size:  18 → 2      (the bug)
volume>=10 via real daily volume: 18 → 18
```

You have live Alpaca paper keys. Before changing a threshold, dump the funnel and see
what actually survives. `ContractSelectionResult.funnel_counts` exists for this.

### 4.4 The same number in two places will bite you

`options_min_volume` lives in BOTH `selection.py` (a heuristic) and `RiskCaps` (the
authoritative veto). Loosening one leaves the other rejecting a layer later — the trap
`selection.py`'s own docstring warns about. Before changing a threshold, grep for it.

### 4.5 Say what you actually verified

Distinguish these, always:
- "Verified live: a council pass returns `BUY NVDA260909C00225000, 4 @ $2.17`"
- "Tests pass, but I could not test the live fill because the market is closed"
- "I believe this is right but did not check"

The user values an honest "I didn't verify that" far above a confident guess. If tests
fail, say so and paste the output. If you skipped something, say which and why.

### 4.6 Fix the cause, not the symptom

When something looks broken, find *why* before patching. The empty-dashboard bug was
not a UI bug — `PostgresDecisionLog` was writing a debug envelope into the `proposal`
column, and that envelope is truthy even when empty, so every HOLD read as "a real
proposal exists" and rendered blank. Patching the UI would have hidden it.

### 4.7 Scope discipline

Do the task asked. If you find a real problem outside that scope, **say so and keep
going** — don't silently widen the change, and don't silently drop part of the ask. If
something is genuinely blocked, finish everything else and state plainly what you left
and why.

---

## 5. Tech stack (locked)

| Layer | Choice |
|---|---|
| Mobile | React Native + Expo + NativeWind + Zustand + TanStack Query |
| Desktop web | Separate tree under `apps/mobile/src/desktop/` (Platinum Glass design system) |
| API | FastAPI + Pydantic v2 + SQLAlchemy 2.0 async + Alembic |
| Agents | LangGraph + **direct Anthropic SDK** (`apps/agents/trading_agents/llm.py`) |
| Broker | Custom abstraction, Alpaca implementation (`packages/broker`) |
| Data | Postgres + Redis · Alpaca (bars, chains, orders) · FRED (macro) |
| Deploy | Railway (`railway up --service AutonomousTradeAgents --detach`) |
| Repo | pnpm workspaces + Turborepo (JS), uv workspaces (Python) |

`litellm` is in the lockfile but **imported zero times**. There is no provider
abstraction; `LLM.complete()` calls Anthropic directly. Don't cite LiteLLM in docs.

**Council cost:** 5 LLM calls/pass (Router+Technical on Haiku; Fundamental, Macro,
Drafter on Sonnet), max 10 with the one re-ask. ~$0.04/pass. `strategy_fit` and
`risk_officer` are deterministic — zero LLM. Cost is not a constraint here; don't
spend hackathon time optimising it.

---

## 6. Git hygiene

- **Land work on `main` directly.** No feature branches unless the user asks.
- Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`).
- Small, focused, one logical change.
- Never `--no-verify`, never `--force`, never `reset --hard` without explicit
  instruction.
- Never commit secrets. `.env` is gitignored; use `.env.example`.
- **End every commit with your identity trailer (§0).**

### Commit messages are the handover protocol

The other model reads these instead of asking the user to repeat themselves. A good
message here answers: what was broken, *how you know*, why the fix is right, and what
you verified. The recent options commits are the reference standard — match them.

### Build log — keep `fable5findings.md` current (REQUIRED)

**After every commit, append to the "Build log" section** of
[`fable5findings.md`](fable5findings.md). The commit isn't done until the entry exists.

```
### <date> — <short-sha> <conventional-commit subject>
- What changed and why.
- What you VERIFIED, and how (command / output).
- Anything left open.
```

Newest entries go at the top of the build log. Don't rewrite the audit sections above
it — append below the `# Build log` heading.

---

## 7. Verification commands

```bash
# Full Python suite — 757 passing, 9 skipped as of 2026-08-29
.venv/bin/python -m pytest apps/agents apps/api packages/ -q

# apps/mcp_server needs `uv sync --all-packages` first, else it fails collection
.venv/bin/python -m pytest apps/ packages/ -q

# Lint (9 pre-existing errors — check the baseline before blaming yourself)
.venv/bin/python -m ruff check <paths>

# TypeScript
pnpm -s exec tsc --noEmit -p apps/mobile/tsconfig.json
pnpm --filter mobile exec jest --silent

# Deploy
railway up --service AutonomousTradeAgents --detach
railway variables --service AutonomousTradeAgents --set "KEY=value"
```

**Check the baseline before attributing a failure to your change:** `git stash`, re-run,
`git stash pop`. Several ruff and mypy errors here predate you.

---

## 8. Working with the user

- They write detailed plans first. Align with them; flag conflicts before implementing.
- **Push back with reasoning when something looks wrong. Don't sycophant.** They have
  explicitly said they value honest assessments over progress reports.
- They are often testing against the live deployment while you work — a bug they report
  is real, current, and worth reproducing before theorising.
- Don't fill in genuinely open questions unilaterally.
- **You may not execute trades**, even on paper. Placing/approving orders is the user's
  action. Build and verify the path; hand them the click.
