"""Risk Officer node — thin adapter over ``engine.risk.evaluate``.

Phase 1 graduation: the deterministic ruleset now lives in
``packages/engine/engine/risk/`` (rules, evaluator, types, context provider).
This node:

  1. Builds a ``RiskProposal`` from the council's draft.
  2. Fetches a ``RiskContext`` from the injected provider
     (``MockRiskContextProvider`` by default; production wires in the real
     reconciler-backed provider).
  3. Calls ``evaluate`` and surfaces the result into ``CouncilState``.

Architecture rule honored: NO LLM here. Risk vetoes are pure Python with
named ``veto_rule`` strings. The PLAN.md §5.1 "Opus refinement" of risk
reasoning is a future, additive layer — it can explain, never override.
"""

from __future__ import annotations

import logging
from typing import cast

from engine.risk import (
    MockRiskContextProvider,
    RiskCaps,
    RiskContextProvider,
    RiskProposal,
    Side,
    SpecialistScore,
    evaluate,
)
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.node.risk")


def _specialists_from_state(state: CouncilState) -> list[SpecialistScore]:
    out: list[SpecialistScore] = []
    for name in ("technical", "fundamental", "macro"):
        s = cast(dict, state.get(name))
        if s and "score" in s:
            out.append(
                SpecialistScore(
                    name=name,
                    score=float(s.get("score", 0.0)),
                    confidence=float(s.get("confidence", 0.0)),
                )
            )
    return out


async def risk_officer_node(
    state: CouncilState,
    caps: RiskCaps | None = None,
    *,
    context_provider: RiskContextProvider | None = None,
) -> CouncilState:
    caps = caps or RiskCaps()
    provider = context_provider or _default_provider(state)

    proposal = state.get("proposal")
    if proposal is None:
        return {
            **state,
            "risk_approved": False,
            "risk_reason": "No proposal — HOLD.",
            "risk_veto_rule": None,
        }

    risk_proposal = RiskProposal(
        symbol=str(state["symbol"]),
        side=Side(str(proposal.get("side", "BUY")).upper()),
        qty=int(proposal.get("qty", 0)),
        estimated_notional=float(proposal.get("estimated_notional", 0.0)),
        last_price=float(state.get("context", {}).get("last_price", 0.0) or 0.0),
        confidence=float(proposal.get("confidence", 0.0)),
        closes_intraday_position=False,  # Phase 0: agents only open new swings
    )

    context = await provider.fetch(user_id=state.get("user_id"))
    decision = evaluate(risk_proposal, context, caps, specialists=_specialists_from_state(state))

    out: CouncilState = {
        **state,
        "risk_approved": decision.approved,
        "risk_reason": decision.reason,
        "risk_veto_rule": decision.veto_rule,
    }

    if not decision.approved:
        out["final_action"] = "VETOED"
        logger.info(
            "risk vetoed %s %s qty=%d via %s — %s",
            risk_proposal.side.value, risk_proposal.symbol, risk_proposal.qty,
            decision.veto_rule, decision.reason,
        )
        return out

    # Approved — may have a trim and/or informational flags.
    new_proposal: dict | None = None
    if decision.adjusted_qty is not None and decision.adjusted_qty != risk_proposal.qty:
        new_proposal = dict(proposal)
        new_proposal["qty"] = decision.adjusted_qty
        new_proposal["estimated_notional"] = round(decision.adjusted_qty * risk_proposal.last_price, 2)
        new_proposal["rationale"] = (
            (proposal.get("rationale") or "")
            + f" (Risk trim: {risk_proposal.qty}→{decision.adjusted_qty})"
        ).strip()

    # Surface non-blocking flags (e.g. wash_sale_warning) onto the proposal
    # so the wire DTO + ApprovalCard can render them. Only forward UI-relevant
    # flags — internal markers like 'trimmed:80->37' stay in the audit log.
    ui_flags = [f for f in decision.informational_flags if not f.startswith("trimmed:")]
    if ui_flags:
        new_proposal = new_proposal if new_proposal is not None else dict(proposal)
        existing = list(new_proposal.get("informational_flags") or [])
        # de-dup while preserving order
        for f in ui_flags:
            if f not in existing:
                existing.append(f)
        new_proposal["informational_flags"] = existing

    if new_proposal is not None:
        out["proposal"] = new_proposal

    return out


def _default_provider(state: CouncilState) -> RiskContextProvider:
    """Pick the provider. ``USE_POSTGRES=1`` → PostgresRiskContextProvider
    reading reconciler-written snapshots; otherwise Mock (synthetic context
    from the feature dict). Same env switch as ``app.services.store``.
    """
    import os

    if os.environ.get("USE_POSTGRES", "").strip().lower() in ("1", "true", "yes", "on"):
        # Lazy import — keeps the agents package light when running offline.
        from engine.db.session import async_session_factory
        from engine.risk import PostgresRiskContextProvider

        return PostgresRiskContextProvider(session_factory=async_session_factory())

    ctx = state.get("context") or {}
    equity = float(ctx.get("portfolio_equity", 100_000.0) or 100_000.0)
    return MockRiskContextProvider(
        account_equity=equity,
        cash=equity,
        buying_power=equity * 2.0,
    )
