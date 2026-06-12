"""Shared state for the council. The dict that flows between nodes.

Per LangGraph idiom: a ``TypedDict`` with ``total=False`` lets each node
contribute new keys without re-declaring the whole shape. Required-on-entry
fields (`symbol`, `horizon`, `context`) are validated by ``runtime.run_council``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict


class CouncilState(TypedDict, total=False):
    # ── Inputs (set by runtime.run_council) ──────────────────────────
    symbol: str
    horizon: Literal["intraday", "short", "mid", "long"]
    triggered_at: datetime
    context: dict[str, Any]
    user_id: str | None

    # ── Router output ────────────────────────────────────────────────
    regime: str
    analyst_subset: list[str]
    router_rationale: str

    # ── Analyst outputs (one dict per specialist) ────────────────────
    technical: dict[str, Any]
    fundamental: dict[str, Any]
    macro: dict[str, Any]

    # ── Reflection-loop priors (injected by runtime when present) ────
    strategy_priors: dict[str, float]
    """``{strategy_id: confidence}`` seeded from the StrategyConfidenceStore.
    The Selector prepends this to its prompt; missing/empty means the LLM
    picks without a prior nudge (Phase 2 cold-start behavior)."""

    # ── Selector output ──────────────────────────────────────────────
    selected_strategy: str | None
    """Strategy id chosen by the Selector node (one of STRATEGY_REGISTRY keys),
    or None when Selector returns HOLD. Drafter is skipped when None."""
    selector_confidence: float
    """0..1 — how confident the Selector is in its pick (separate from the
    Drafter's per-trade confidence)."""
    selector_rationale: str

    # ── Drafter output ───────────────────────────────────────────────
    proposal: dict[str, Any] | None

    # ── Risk officer (deterministic) ─────────────────────────────────
    risk_approved: bool
    risk_reason: str
    risk_veto_rule: str | None

    # ── Final ────────────────────────────────────────────────────────
    final_action: Literal["BUY", "SELL", "HOLD", "VETOED"]
    token_usage: dict[str, int]

    # ── Degradation audit ────────────────────────────────────────────
    degraded_nodes: list[str]
    """Nodes whose LLM output was malformed and ran on a retry or a neutral
    fallback this pass. Recorded on the decision row so calibration /
    reflection can exclude degraded runs instead of learning from them."""
