# apps/agents

LangGraph state-machine council. Nodes propose; nothing here decides.

## Status — Phase 2 complete (Reflection loop closed)

**Implemented nodes** (`apps/agents/trading_agents/nodes/`):
- `router_node` — picks regime + analyst subset (Haiku)
- `technical_analyst_node` — Haiku
- `fundamental_analyst_node` — Sonnet
- `macro_analyst_node` — Sonnet (Phase 2 addition: VIX / 10y / DXY / sector RS)
- `selector_node` — Haiku-tier; picks a strategy id from `STRATEGY_REGISTRY` or HOLDs. Unknown id → fallback to `momentum` with capped confidence. HOLD short-circuits the graph (Drafter is skipped). **Reads `strategy_priors` from state when the runtime wires a confidence store** — Reflection's nudges land here.
- `drafter_node` — Sonnet-tier; reads Selector's pick + analyst output, builds verdict + bull/bear narrative + risk/conviction levels. Ignores any LLM-emitted qty, delegates sizing to `engine.sizing.atr_position_size`.
- `risk_officer_node` — thin adapter over `engine.risk.evaluate` (deterministic; NO LLM)
- `reflection_agent_run` — **out-of-band**. Reads completed `DecisionLog` entries (rows with `realized_pnl` set + `reviewed_at` unset), batches by strategy, asks Sonnet for `{wins, losses, lessons, confidence_delta}`, writes a clamped delta (±0.10/cycle, abs bounds 0.05–0.95) into the `StrategyConfidenceStore`. NEVER writes to broker / risk / executor.

**Memory layer** (`trading_agents/memory/`):
- `DecisionLog` protocol + `InMemoryDecisionLog` + **`PostgresDecisionLog`** (Phase 3.4 follow-on). One row per `run_council` pass when the runtime is wired with a log. Fields cover Selector pick + scores + final action + the `proposal` slice of state for replay.
- `StrategyConfidenceStore` protocol + `InMemoryStrategyConfidenceStore` + **`PostgresStrategyConfidenceStore`**. Per-strategy priors seeded at 0.5. Constants `MAX_CONFIDENCE_DELTA_PER_CYCLE=0.10`, `MIN_CONFIDENCE=0.05`, `MAX_CONFIDENCE=0.95` make the loop stable under small-N noise.
- `get_decision_log()` + `get_confidence_store()` factories pick the impl by `USE_POSTGRES` env. Default = in-memory; flip to Postgres with `USE_POSTGRES=1` once `make migrate` has applied schema 0003.

**Strategy registry** (`trading_agents/strategies/__init__.py`) — agent-side id ↔ metadata map. Decoupled from the backtester strategy implementations under `packages/engine/engine/backtester/strategies/`. The id is the only contract between council and backtester.

**PLAN.md §5.1 council**: 7/7 specialist nodes shipped. The loop closes — Reflection updates Selector priors; Selector reads them on the next pass. Phase 3 is now the natural next phase (mobile auth + biometric + push).

## Mock-mode + real-LLM switching

`trading_agents.llm.LLM` auto-detects `ANTHROPIC_API_KEY`:
- Empty or unset → **mock mode**. Returns canned JSON keyed on the prompt's `You are the <Role>` line. Council runs offline, no cents burned.
- Real key → uses `AsyncAnthropic` with prompt caching on the system block (5-min TTL).

Strip the env var to flip:
```bash
ANTHROPIC_API_KEY=  python -m trading_agents --symbol NVDA  # MOCK
ANTHROPIC_API_KEY=sk-ant-...  python -m trading_agents --symbol NVDA  # REAL
```

Empty-string env (`ANTHROPIC_API_KEY=""`) is treated as missing — fixed in Phase 2 kickoff after a real bug.

## Architecture rule

**Agents propose, deterministic code disposes.**
- Risk vetoes happen in `packages/engine/risk`, not here. LLM reasoning at the Risk Officer node may *refine* an explanation, never *override* a deterministic block.
- Order placement happens via `packages/broker`, called from the Executor node — never from an analyst.
- Analysts read pre-computed features. No raw market-data calls inside this package.

## Model tiers (per PLAN.md §5.1)

| Node | Model |
|---|---|
| Router | Haiku 4.5 |
| Technical Analyst | Haiku 4.5 / Sonnet 4.6 |
| Fundamental Analyst | Sonnet 4.6 |
| Macro Analyst | Sonnet 4.6 |
| Strategy Selector | Haiku 4.5 (cheap pick) |
| Proposal Drafter | Sonnet 4.6 (heavy narrative) |
| Risk Officer (LLM refinement, Phase 2.5) | Opus 4.7 |
| Executor | Haiku 4.5 |
| Reflection | Sonnet 4.6 |

LLM calls go through one wrapper (`trading_agents.llm.LLM`) so prompt caching + cost telemetry land in a single place. LiteLLM is on the dependency list for Phase 2.5 when a cost ledger gets wired.

## Tests

```bash
# Mock-LLM (always runs):
PYTHONPATH=apps/agents:packages/engine:packages/broker pytest apps/agents/tests/ -v

# Real-LLM smoke (opt-in; costs ~$0.001 with Haiku):
RUN_REAL_LLM_TESTS=1 ANTHROPIC_API_KEY=sk-ant-... pytest apps/agents/tests/ -v
```

## CLIs

**Council** — one decision per invocation:

```bash
PYTHONPATH=apps/agents:packages/engine:packages/broker \
  python -m trading_agents --symbol NVDA --no-langgraph
```

Output covers Router → Technical → Fundamental → Macro → Selector → Drafter → Risk Officer with stop / target / informational flags surfaced on the proposal DTO. If the Selector returns HOLD, the Drafter is skipped and `final_action=HOLD`.

**Reflection** — out-of-band review:

```bash
PYTHONPATH=apps/agents:packages/engine:packages/broker \
  python -m trading_agents.reflection_cli --since 24h
```

Seeds three demo decisions, runs the Reflection loop, prints the per-strategy summary + current priors. Pass `--no-seed` against a real `DecisionLog` (post-Phase 3). Reflection NEVER runs inside `run_council` — keep this entry point separate.
