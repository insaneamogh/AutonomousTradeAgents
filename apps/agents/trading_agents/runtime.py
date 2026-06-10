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

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from trading_agents.features import synthetic_features
from trading_agents.graph import run_graph
from trading_agents.llm import LLM
from trading_agents.memory import (
    DecisionEntry,
    DecisionLog,
    StrategyConfidenceStore,
)
from trading_agents.progress import ProgressCallback
from engine.risk import RiskCaps
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.runtime")


# Approval expiry: Phase 0 default. Strategy-Selector can override later.
DEFAULT_APPROVAL_TTL = timedelta(minutes=15)


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
) -> dict[str, Any]:
    """Run the full council. Returns a result dict:

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
    state: CouncilState = {
        "symbol": symbol.upper(),
        "horizon": horizon,
        "triggered_at": datetime.now(timezone.utc),
        "user_id": user_id,
        "context": feature_provider(symbol.upper(), horizon),
    }
    if confidence_store is not None:
        # Selector pulls its priors out of state. We resolve once here so the
        # node stays a pure function of the state dict + LLM.
        state["strategy_priors"] = {
            row.strategy_id: row.confidence for row in await confidence_store.all()
        }

    final = await run_graph(
        state,
        llm=llm,
        risk_caps=risk_caps,
        progress_cb=progress_cb,
        # Pace only in MOCK mode — real LLM calls are their own pacing.
        pacing_seconds=pacing_seconds if llm.mock else 0.0,
    )

    proposal_dto = _to_proposal_dto(final) if final.get("risk_approved") else None

    decision_id: str | None = None
    if decision_log is not None:
        # Fire-and-forget write. We await it here (not via asyncio.create_task)
        # so callers can read decision_id from the result; the in-memory log
        # is sync-fast anyway, and a real Postgres impl will be wrapped in
        # asyncio.shield by the caller if it wants true fire-and-forget.
        entry = _to_decision_entry(state["symbol"], horizon, user_id, final, proposal_dto)
        recorded = await decision_log.record(entry)
        decision_id = recorded.id

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
        "selector_confidence": float(final.get("selector_confidence", 0.0)),
        "selector_rationale": str(final.get("selector_rationale", "")),
        "llm_mock": llm.mock,
        "decision_id": decision_id,
    }


def _to_proposal_dto(state: CouncilState) -> dict[str, Any] | None:
    p = state.get("proposal")
    if not p:
        return None
    now = datetime.now(timezone.utc)
    return {
        "id": f"agent-{uuid.uuid4().hex[:12]}",
        "symbol": state["symbol"],
        "side": p["side"],
        "qty": int(p["qty"]),
        "orderType": p.get("order_type", "MARKET"),
        "limitPrice": p.get("limit_price"),
        "estimatedNotional": float(p["estimated_notional"]),
        "stopLoss": p.get("stop_loss"),
        "targetPrice": p.get("target_price"),
        "informationalFlags": list(p.get("informational_flags") or []),
        "rationale": p.get("rationale", ""),
        "bullCase": p.get("bull_case", ""),
        "bearCase": p.get("bear_case", ""),
        "riskLevel": int(p.get("risk_level", 3)),
        "convictionLevel": int(p.get("conviction_level", 3)),
        "proposedAt": now.isoformat(),
        "expiresAt": (now + DEFAULT_APPROVAL_TTL).isoformat(),
    }


def _to_decision_entry(
    symbol: str,
    horizon: str,
    user_id: str | None,
    final: CouncilState,
    proposal_dto: dict[str, Any] | None,
) -> DecisionEntry:
    """Build the audit row from the final council state.

    Keeps ``raw_state`` tight — we drop ``context`` (potentially big +
    redundant with the per-analyst scores) and stash the proposal under a
    flat key so the Reflection prompt can pull bull/bear out without a
    deep walk.
    """
    tech = final.get("technical") or {}
    fund = final.get("fundamental") or {}
    macro = final.get("macro") or {}
    internal_proposal = final.get("proposal") or {}

    return DecisionEntry(
        user_id=user_id,
        symbol=symbol,
        horizon=horizon,
        triggered_at=final.get("triggered_at") or datetime.now(timezone.utc),
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
            "proposal": final.get("proposal"),
            "regime": final.get("regime"),
            "analyst_subset": final.get("analyst_subset"),
        },
        # Full audit surface (WP0) — dedicated columns in Postgres.
        technical=tech or None,
        fundamental=fund or None,
        macro=macro or None,
        analyst_subset=list(final.get("analyst_subset") or []) or None,
        bull_case=internal_proposal.get("bull_case") or (proposal_dto or {}).get("bullCase"),
        bear_case=internal_proposal.get("bear_case") or (proposal_dto or {}).get("bearCase"),
        risk_reason=str(final.get("risk_reason") or "") or None,
        token_usage=final.get("token_usage"),
        completed_at=datetime.now(timezone.utc),
        proposal_dto=proposal_dto,
    )
