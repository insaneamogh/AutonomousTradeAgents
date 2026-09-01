"""Council runtime — the entry point apps/api calls.

``run_council(symbol, horizon)`` is the single public function. Pulls
features, runs the graph, returns either an ApprovalProposalDto-shaped dict
(when risk approved) or None (HOLD / VETOED).

DTO conversion happens here so the API router stays thin and so the same
shape works for both Phase 0 (in-memory store) and Phase 1 (Postgres).

Phase 2 finale: optional ``decision_log`` + ``confidence_store`` kwargs
enable the Reflection loop. The runtime writes one ``DecisionEntry`` per
council pass and the Selector reads the current priors. Both default to
None — the council runs identically without them; you opt in by passing a
log instance (typically one-per-process in the API or one-per-CLI-invocation).
"""

from __future__ import annotations

import inspect
import logging
import os
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal

from engine.risk import RiskCaps
from trading_agents.features import synthetic_features
from trading_agents.graph import run_graph
from trading_agents.llm import LLM
from trading_agents.memory import (
    DecisionEntry,
    DecisionLog,
    StrategyConfidenceStore,
)
from trading_agents.progress import ProgressCallback
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.runtime")


# Approval expiry. This is a SWING product (1-10 day holds) — a proposal at
# market open should survive until the close, not die in 15 minutes while
# the user is in a meeting (audit finding: cron proposals expired unseen).
# Default: 21:00 UTC same day (≥ NYSE close year-round; EDT close is 20:00
# UTC). Override with AGENT_APPROVAL_TTL_MINUTES for tests / intraday work.
_MARKET_DAY_END_UTC = time(21, 0)


def approval_expiry(now: datetime) -> datetime:
    override = os.environ.get("AGENT_APPROVAL_TTL_MINUTES", "").strip()
    if override:
        return now + timedelta(minutes=float(override))
    candidate = datetime.combine(now.date(), _MARKET_DAY_END_UTC, tzinfo=UTC)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def run_council(
    *,
    symbol: str,
    horizon: Literal["intraday", "short", "mid", "long"] = "short",
    user_id: str | None = None,
    llm: LLM | None = None,
    risk_caps: RiskCaps | None = None,
    feature_provider=synthetic_features,
    decision_log: DecisionLog | None = None,
    confidence_store: StrategyConfidenceStore | None = None,
    progress_cb: ProgressCallback | None = None,
    pacing_seconds: float = 0.0,
    instrument_preference: Literal["equity", "option"] | None = None,
) -> dict[str, Any]:
    """Run the full council. Returns a result dict:

    ``instrument_preference`` is the caller-facing surface for Phase A
    options trading (e.g. a watchlist item's ``asset_class``): threaded
    straight into ``CouncilState`` for ``strategy_fit_node`` to read, which
    also requires ``ALLOW_OPTIONS`` before treating a run as options-
    eligible — neither flag alone is enough. ``None``/``"equity"`` is the
    only behavior this ever had before Phase A.

    {
        "proposal": <ApprovalProposalDto-shape, camelCase keys> | None,
        "final_action": "BUY" | "SELL" | "HOLD" | "VETOED",
        "risk_approved": bool,
        "risk_reason": str,
        "risk_veto_rule": str | None,
        "regime": str | None,
        "technical": {...} | None,
        "fundamental": {...} | None,
        "llm_mock": bool,
        "decision_id": <id when decision_log was passed, else None>,
    }
    """
    llm = llm or LLM()
    # Generated BEFORE any LLM call in this pass — agent_decisions.id does not
    # exist yet (it's assigned only after decision_log.record() below, which
    # runs strictly after run_graph() completes). Every node's complete_json()
    # call carries this so the cost ledger can correlate rows to this pass
    # now and backfill the real decision id onto them afterwards. See
    # trading_agents.cost_ledger.CostLedger.backfill_decision_id.
    council_run_id = str(uuid.uuid4())
    # Feature providers may be sync (synthetic) or async (real Alpaca/FRED
    # provider) — await when needed so callers don't care which they wired.
    context = feature_provider(symbol.upper(), horizon)
    if inspect.isawaitable(context):
        context = await context
    state: CouncilState = {
        "symbol": symbol.upper(),
        "horizon": horizon,
        "triggered_at": datetime.now(UTC),
        "user_id": user_id,
        "council_run_id": council_run_id,
        "context": context,
        "instrument_preference": instrument_preference,
    }
    if confidence_store is not None:
        # Selector pulls its priors out of state. We resolve once here so the
        # node stays a pure function of the state dict + LLM.
        state["strategy_priors"] = {
            row.strategy_id: row.confidence for row in await confidence_store.all()
        }

    # Wrap the whole pass in a Langfuse trace — each agent node's LLM call
    # nests under it as a generation (router / technical / … / drafter), so
    # you can see what every agent did and whether it succeeded, ran
    # degraded, or failed. No-op when Langfuse keys are unset.
    from trading_agents.tracing import council_trace
    from trading_agents.tracing import flush as _trace_flush

    try:
        with council_trace(
            symbol=state["symbol"], horizon=horizon, user_id=user_id
        ) as trace:
            final = await run_graph(
                state,
                llm=llm,
                risk_caps=risk_caps,
                progress_cb=progress_cb,
                # Pace only in MOCK mode — real LLM calls are their own pacing.
                pacing_seconds=pacing_seconds if llm.mock else 0.0,
            )
            trace.set_output(
                output={
                    "final_action": final.get("final_action"),
                    "risk_approved": bool(final.get("risk_approved", False)),
                    "risk_veto_rule": final.get("risk_veto_rule"),
                    "selected_strategy": final.get("selected_strategy"),
                },
                metadata={"degraded_nodes": list(final.get("degraded_nodes") or [])},
            )
    finally:
        # Short-lived processes (the cron) need the export kicked before
        # exit; the long-lived API relies on the SDK's background flush but
        # an extra flush here is harmless.
        _trace_flush()

    proposal_dto = _to_proposal_dto(final) if final.get("risk_approved") else None

    decision_id: str | None = None
    # The options council's trade tool persists its OWN agent_decisions row,
    # keyed on the SAME council_run_id this function would use. Writing
    # again here would land a summary on top of a real executed trade and
    # erase the fill, the order id and the risk checks it recorded. So when
    # that node says a row exists, take its id and do not write.
    if final.get("decision_row_written"):
        decision_id = str(final.get("decision_id") or council_run_id)
    elif decision_log is not None:
        # Fire-and-forget write. We await it here (not via asyncio.create_task)
        # so callers can read decision_id from the result; the in-memory log
        # is sync-fast anyway, and a real Postgres impl will be wrapped in
        # asyncio.shield by the caller if it wants true fire-and-forget.
        entry = _to_decision_entry(
            state["symbol"], horizon, user_id, final, proposal_dto, council_run_id
        )
        recorded = await decision_log.record(entry)
        decision_id = recorded.id
        # Best-effort: attach the now-real decision id to every llm_calls row
        # this pass wrote under council_run_id. A ledger outage must never
        # take down a council run — same convention as every other
        # telemetry write in this codebase (see llm._record_to_ledger).
        try:
            from trading_agents.cost_ledger import get_cost_ledger

            await get_cost_ledger().backfill_decision_id(
                council_run_id=council_run_id, decision_id=decision_id
            )
        except Exception as exc:
            logger.warning("cost ledger backfill failed (best-effort): %s", exc)

    return {
        "proposal": proposal_dto,
        "final_action": final.get("final_action", "HOLD"),
        "risk_approved": bool(final.get("risk_approved", False)),
        "risk_reason": str(final.get("risk_reason", "")),
        "risk_veto_rule": final.get("risk_veto_rule"),
        "regime": final.get("regime"),
        "technical": final.get("technical"),
        "fundamental": final.get("fundamental"),
        "macro": final.get("macro"),
        # Selector surface — useful for the mobile reasoning panel and for the
        # Reflection Agent that will score Selector decisions against outcomes.
        "selected_strategy": final.get("selected_strategy"),
        "selected_direction": final.get("selected_direction"),
        "selector_confidence": float(final.get("selector_confidence", 0.0)),
        "selector_rationale": str(final.get("selector_rationale", "")),
        "strategy_fit": final.get("strategy_fit"),
        "risk_checks_passed": list(final.get("risk_checks_passed") or []),
        # The same deterministic block that is persisted on the decision
        # row. Returned here too so a caller that runs a pass directly (the
        # API's council endpoint, the MCP server, a test) sees the contract
        # funnel and the trim attribution without a second DB read.
        "reasoning": _reasoning_block(final),
        "llm_mock": llm.mock,
        "decision_id": decision_id,
    }


def _opt_float(v: object) -> float | None:
    """None stays None — a missing confidence must not become 0.0, which
    would veto at the floor instead of self-gating the rule out."""
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_proposal_dto(state: CouncilState) -> dict[str, Any] | None:
    p = state.get("proposal")
    if not p:
        return None
    now = datetime.now(UTC)
    # Short-side facts. ``direction``/``opens_short`` come from the Drafter
    # (state["proposal"]); ``shortable``/``easy_to_borrow`` are the broker's
    # asset flags, carried on the feature dict the same way risk_officer_node
    # reads them — never from the LLM.
    asset = (state.get("context", {}) or {}).get("asset") or {}
    dto: dict[str, Any] = {
        "id": f"agent-{uuid.uuid4().hex[:12]}",
        "symbol": state["symbol"],
        "side": p["side"],
        "direction": p.get("direction", "long"),
        "opensShort": bool(p.get("opens_short", False)),
        "qty": int(p["qty"]),
        "orderType": p.get("order_type", "MARKET"),
        "limitPrice": p.get("limit_price"),
        "estimatedNotional": float(p["estimated_notional"]),
        "stopLoss": p.get("stop_loss"),
        "targetPrice": p.get("target_price"),
        "timeStopDays": int(p.get("time_stop_days", 5)),
        "rMultiple": p.get("r_multiple"),
        "informationalFlags": list(p.get("informational_flags") or []),
        "rationale": p.get("rationale", ""),
        "bullCase": p.get("bull_case", ""),
        "bearCase": p.get("bear_case", ""),
        "riskLevel": int(p.get("risk_level", 3)),
        "convictionLevel": int(p.get("conviction_level", 3)),
        # The council's confidence (0-1), carried explicitly. Conviction
        # (1-5, "how big a bet") is NOT a substitute for it ("how likely to
        # work") and the two are emitted separately by the Drafter. Until
        # this key existed the executor's approval-time re-check could not
        # find the real number and fell back to conviction_level/5, so a
        # pick drafted at confidence 0.54 with conviction 2 surfaced as
        # approvable and was then refused as "0.40 below floor 0.42".
        # Live at the time of the fix: 0 of 30 approved rows carried it.
        "councilConfidence": _opt_float(p.get("confidence")),
        "shortable": asset.get("shortable"),
        "easyToBorrow": asset.get("easy_to_borrow"),
        "proposedAt": now.isoformat(),
        "expiresAt": approval_expiry(now).isoformat(),
    }
    # Options facts (Phase A). ``ApprovalProposalDto.model_validate()`` (see
    # apps/api/app/routers/agent.py) defaults every one of these when the
    # key is absent, so an equity proposal is unaffected either way — but an
    # OPTIONS proposal's fields must be threaded through explicitly here or
    # they are silently dropped at exactly this boundary (Pydantic ignores
    # unknown/omitted keys by default) even though the Drafter wrote them
    # into ``p`` correctly. This is the same class of bug the short-side
    # work spent 5 commits chasing: a field one layer produces and the next
    # layer forgets to carry. Only set when the Drafter actually marked
    # this an options proposal — never invent option facts for an equity one.
    if p.get("is_option"):
        dto.update(
            {
                "isOption": True,
                "optionAction": p.get("option_action"),
                "occSymbol": p.get("occ_symbol"),
                "strike": p.get("strike"),
                "expiryDate": p.get("expiry_date"),
                "contractType": p.get("contract_type"),
                "multiplier": p.get("multiplier", 100),
                # Extra option-snapshot fields (bid/ask/OI/volume/IV/days-
                # to-earnings) an options risk rule needs at execution time
                # for a fresh liquidity/earnings re-check — see
                # ApprovalProposalDto and drafter._draft_option_proposal.
                "openInterest": p.get("open_interest"),
                "volume": p.get("volume"),
                "bid": p.get("bid"),
                "ask": p.get("ask"),
                "impliedVolatility": p.get("implied_volatility"),
                "daysToEarnings": p.get("days_to_earnings"),
            }
        )
    return dto


# Feature blocks worth keeping on the decision row. The full context is
# large and mostly redundant with the analyst theses; these four are the
# ones the thesis view actually renders and the ones a post-mortem needs to
# reconstruct what the machine was looking at.
_SNAPSHOT_BLOCKS = ("technicals", "quant", "patterns", "news", "events", "liquidity", "asset")


def _reasoning_block(final: CouncilState) -> dict[str, Any]:
    """The deterministic reasoning surface, persisted for the thesis view.

    Everything in here is machine output, not model prose: which strategy
    fit and by which NAMED checks, which risk rules passed (not only the
    one that vetoed), what woke the scanner, the sizing arithmetic behind
    the qty/stop/target, and the features every analyst was reading.

    It gets its own column rather than riding in ``raw_state`` because the
    Postgres log writes ``raw_state`` into ``proposal`` ONLY when there is
    no approved proposal — which dropped all of this on exactly the
    approved decisions a user asks about.
    """
    proposal = final.get("proposal") or {}
    return {
        "version": 1,
        "strategy_fit": final.get("strategy_fit"),
        "selected_direction": final.get("selected_direction"),
        "router_rationale": final.get("router_rationale"),
        "analyst_subset": list(final.get("analyst_subset") or []),
        "risk_checks_passed": list(final.get("risk_checks_passed") or []),
        "risk_veto_rule": final.get("risk_veto_rule"),
        # Partial refusals. Separate from ``risk_veto_rule`` so no reader
        # can mistake "risk shrank this" for "risk blocked this".
        "risk_trim_rules": list(final.get("risk_trim_rules") or []),
        "risk_reason": final.get("risk_reason"),
        # The Drafter's own explanation of a HOLD — present only when the
        # model actually reached a verdict (constrained by the fit node's
        # direction) and said no, or the sizer zeroed out its qty. Absent
        # for the two upstream HOLDs (no strategy fit; parse failure).
        "drafter_rationale": final.get("drafter_rationale") or None,
        "sizing": proposal.get("sizing"),
        "informational_flags": list(proposal.get("informational_flags") or []),
        "contract_funnel": final.get("contract_funnel"),
        # The two-agent options debate: each side's direction and
        # conviction, plus how the deterministic resolver combined them.
        # Absent on every equity pass (that leg never runs) and on an
        # options pass with USE_OPTIONS_AGENT off.
        #
        # Persisted because it was otherwise invisible: `options_bull` and
        # `options_bear` were both firing in production (45 and 21 calls
        # over three days) while nothing outside the process log could
        # tell whether they had argued, agreed, or abstained — so a HOLD
        # produced by two agents disagreeing looked identical to a HOLD
        # produced by no agent running at all.
        "options_resolution": final.get("options_resolution"),
        # Named refusals the tool guard returned this pass, e.g.
        # "open_option_trade:illiquid_contract". The propose/dispose story
        # in one line, and the only record of it outside the log.
        "tool_denials": list(final.get("tool_denials") or []) or None,
        "scan_triggers": (final.get("context") or {}).get("scan_triggers"),
        "feature_snapshot": _feature_snapshot(final.get("context") or {}),
        "degraded_nodes": list(final.get("degraded_nodes") or []),
    }


def _feature_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    """The slice of the feature dict worth persisting alongside a decision.

    Not the whole context: it carries 200+ bars' worth of derived numbers
    per symbol and would bloat every audit row. These blocks are the ones a
    reader needs to check the machine's homework.
    """
    snap: dict[str, Any] = {
        k: context[k] for k in _SNAPSHOT_BLOCKS if isinstance(context.get(k), dict)
    }
    if context.get("last_price") is not None:
        snap["last_price"] = context["last_price"]
    if context.get("portfolio_equity") is not None:
        snap["portfolio_equity"] = context["portfolio_equity"]
    return snap


def _to_decision_entry(
    symbol: str,
    horizon: str,
    user_id: str | None,
    final: CouncilState,
    proposal_dto: dict[str, Any] | None,
    council_run_id: str,
) -> DecisionEntry:
    """Build the audit row from the final council state.

    Keeps ``raw_state`` tight — we drop ``context`` (potentially big +
    redundant with the per-analyst scores) and stash the proposal under a
    flat key so the Reflection prompt can pull bull/bear out without a
    deep walk.

    ``id=council_run_id`` (rather than letting ``DecisionEntry``'s own
    default id factory fire) ties this decision's row id to the same value
    every LLM call in this pass was correlated under, so
    ``PostgresDecisionLog.record()`` can reuse it as the real row id and the
    cost-ledger backfill and this decision agree on one id with no lookup.
    """
    tech = final.get("technical") or {}
    fund = final.get("fundamental") or {}
    macro = final.get("macro") or {}
    internal_proposal = final.get("proposal") or {}
    # Persisted `proposal` must be the same camelCase shape regardless of
    # risk_approved. `proposal_dto` is already that shape when approved;
    # when vetoed it's None (see run_council), and without this the
    # fallback in PostgresDecisionLog.record() would persist
    # `internal_proposal` untouched — the Drafter's snake_case dict, whose
    # `estimated_notional` no camelCase reader (ghost_eval, the veto
    # ledger) will ever find under `estimatedNotional`.
    audit_proposal = proposal_dto if proposal_dto is not None else _to_proposal_dto(final)

    return DecisionEntry(
        id=council_run_id,
        user_id=user_id,
        symbol=symbol,
        horizon=horizon,
        triggered_at=final.get("triggered_at") or datetime.now(UTC),
        regime=final.get("regime"),
        selected_strategy=final.get("selected_strategy"),
        selector_confidence=float(final.get("selector_confidence", 0.0)),
        selector_rationale=str(final.get("selector_rationale", "")),
        final_action=str(final.get("final_action", "HOLD")),
        proposal_id=(proposal_dto or {}).get("id"),
        risk_approved=bool(final.get("risk_approved", False)),
        risk_veto_rule=final.get("risk_veto_rule"),
        technical_score=float(tech.get("score")) if tech.get("score") is not None else None,
        fundamental_score=float(fund.get("score")) if fund.get("score") is not None else None,
        macro_score=float(macro.get("score")) if macro.get("score") is not None else None,
        raw_state={
            "proposal": audit_proposal,
            "regime": final.get("regime"),
            "analyst_subset": final.get("analyst_subset"),
            # Non-empty when any node ran on a parse-retry or neutral
            # fallback — calibration/reflection exclude these runs.
            "degraded_nodes": list(final.get("degraded_nodes") or []),
        },
        reasoning=_reasoning_block(final),
        # Full audit surface (WP0) — dedicated columns in Postgres.
        technical=tech or None,
        fundamental=fund or None,
        macro=macro or None,
        analyst_subset=list(final.get("analyst_subset") or []) or None,
        # Third fallback: the Drafter's own bull/bear case survives on a
        # HOLD via ``final["bull_case"]``/``final["bear_case"]`` (see
        # drafter_node) — without it, every HOLD the model actually
        # explained looked identical to one it never reasoned about at all.
        bull_case=(
            internal_proposal.get("bull_case")
            or (proposal_dto or {}).get("bullCase")
            or final.get("bull_case")
            or None
        ),
        bear_case=(
            internal_proposal.get("bear_case")
            or (proposal_dto or {}).get("bearCase")
            or final.get("bear_case")
            or None
        ),
        risk_reason=str(final.get("risk_reason") or "") or None,
        token_usage=final.get("token_usage"),
        completed_at=datetime.now(UTC),
        degraded_nodes=list(final.get("degraded_nodes") or []) or None,
        proposal_dto=proposal_dto,
    )
