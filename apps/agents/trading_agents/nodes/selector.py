"""Selector node — picks a strategy id (Haiku-tier).

PLAN.md §5.1 splits Strategy Selector from Proposal Drafter so the cheap
"pick" runs on a Haiku-tier model and the heavier "draft narrative" runs on
Sonnet. Selector also gates the council: HOLD here short-circuits the
graph (Drafter is skipped) and ``CouncilState.final_action = "HOLD"``.

Selector validates its output against ``trading_agents.strategies.STRATEGY_REGISTRY``
— unknown ids fall back to ``momentum`` with reduced confidence rather than
crashing the council on a hallucinated id.
"""

from __future__ import annotations

import logging

from trading_agents.llm import LLM, Model
from trading_agents.prompts import SELECTOR
from trading_agents.state import CouncilState
from trading_agents.strategies import STRATEGY_REGISTRY

logger = logging.getLogger("agents.node.selector")


async def selector_node(state: CouncilState, llm: LLM) -> CouncilState:
    tech = state.get("technical")
    fund = state.get("fundamental")
    macro = state.get("macro")

    parts: list[str] = [
        f"Ticker: {state['symbol']}",
        f"Horizon: {state.get('horizon', 'short')}",
        f"Regime: {state.get('regime', 'unknown')}",
        "",
    ]
    for label, payload in (("Technical", tech), ("Fundamental", fund), ("Macro", macro)):
        if payload:
            parts.append(
                f"{label} analyst: score={payload.get('score', 'n/a')} "
                f"conf={payload.get('confidence', 'n/a')} thesis=\"{payload.get('thesis', '')}\""
            )

    # Reflection-loop priors. When present we append a small table so the
    # LLM weighs strategies by their recent track record. Missing means the
    # runtime didn't wire a confidence store this pass — cold-start picks.
    priors = state.get("strategy_priors") or {}
    if priors:
        parts.append("")
        parts.append("Strategy priors (Reflection Agent — weight your pick by these):")
        for sid in sorted(priors, key=lambda k: priors[k], reverse=True):
            parts.append(f"  - {sid}: {priors[sid]:.2f}")

    user = "\n".join(parts) + "\n"

    resp = await llm.complete(system=SELECTOR, user=user, model=Model.HAIKU, max_tokens=300)
    try:
        data = LLM.parse_json(resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("selector parse failed: %s — HOLD", exc)
        return _hold(state, "Selector parse error.")

    strategy_raw = data.get("strategy")
    confidence = float(data.get("confidence", 0.0) or 0.0)
    rationale = str(data.get("rationale", "")).strip()

    # null / missing / unknown → HOLD.
    if strategy_raw is None or strategy_raw == "" or strategy_raw == "null":
        return _hold(state, rationale or "No strategy fits — HOLD.")

    if strategy_raw not in STRATEGY_REGISTRY:
        logger.warning(
            "selector returned unknown strategy %r — falling back to momentum @ confidence=0.3",
            strategy_raw,
        )
        strategy_id = "momentum"
        confidence = min(confidence, 0.3)
        rationale = f"(fallback from unknown id {strategy_raw!r}) {rationale}"
    else:
        strategy_id = strategy_raw

    return {
        **state,
        "selected_strategy": strategy_id,
        "selector_confidence": max(0.0, min(1.0, confidence)),
        "selector_rationale": rationale,
    }


def _hold(state: CouncilState, rationale: str) -> CouncilState:
    return {
        **state,
        "selected_strategy": None,
        "selector_confidence": 0.0,
        "selector_rationale": rationale,
        "proposal": None,
        "final_action": "HOLD",
    }
