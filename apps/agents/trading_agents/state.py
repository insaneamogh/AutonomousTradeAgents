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
    council_run_id: str
    """Generated once per pass, before any LLM call (``runtime.run_council``).
    Correlates every ``llm_calls`` row this pass writes so cost can be
    attributed to the eventual ``agent_decisions`` row before that row's id
    exists — see ``trading_agents.cost_ledger.CostLedger.backfill_decision_id``."""

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

    instrument_preference: Literal["equity", "option"] | None
    """Per-run caller preference (e.g. a watchlist item's ``asset_class``),
    read by ``strategy_fit_node``. Additive, optional — absent means
    "no preference" (equity, same as always). Gated by BOTH this AND the
    ``ALLOW_OPTIONS`` env flag before anything downstream treats a run as
    options-eligible; neither one alone is enough."""

    # ── Strategy-fit output (deterministic; keys kept from the old
    #     LLM Selector so the DB columns + Reflection loop are unchanged) ──
    selected_strategy: str | None
    """Strategy id chosen by ``strategy_fit_node`` (one of STRATEGY_REGISTRY
    keys), or None when nothing cleared the fit floor. When None the whole
    rest of the graph is skipped — including every LLM call."""
    selector_confidence: float
    """0..1 — the prior-adjusted fit score. Deterministic, not a model's
    self-report, and separate from the Drafter's per-trade confidence."""
    selector_rationale: str
    """Named reason, e.g. ``momentum_short:trailing_3m_return+risk_adjusted``."""
    selected_direction: str | None
    """"long" | "short". Fixed deterministically BEFORE the Drafter runs, so
    the Drafter cannot invent a direction the preconditions don't support."""
    strategy_fit: dict[str, Any]
    """Full fit block — winner, per-component checks, the ranked
    alternatives, and the priors applied. Persisted for the audit row and
    rendered by the thesis view."""

    instrument: Literal["option"]
    """Set by ``strategy_fit_node``, additive, ONLY when ``ALLOW_OPTIONS``
    + ``instrument_preference == "option"`` are both set and a strategy
    actually won (never on a HOLD). Absent means "equity", the only value
    this ever took before Phase A. Read by ``drafter_node`` to switch from
    ``atr_position_size`` to ``select_contract`` + ``options_position_size``."""

    # ── Drafter output ───────────────────────────────────────────────
    proposal: dict[str, Any] | None
    drafter_rationale: str
    """The Drafter's own explanation of a HOLD verdict — set ONLY when the
    model actually reached a verdict and said no, or the sizer zeroed its
    qty (see ``drafter_node``). Absent for the two upstream HOLDs (no
    strategy fit; parse failure), where there was no verdict to explain."""
    bull_case: str
    """The Drafter's bull case, kept even on a HOLD — normally this rides
    inside ``proposal``, but a HOLD never builds one."""
    bear_case: str
    """The Drafter's bear case, kept even on a HOLD — same reasoning as
    ``bull_case`` above."""

    # ── Risk officer (deterministic) ─────────────────────────────────
    risk_approved: bool
    risk_reason: str
    risk_veto_rule: str | None
    risk_checks_passed: list[str]
    """Named rules that ran and did not block, in evaluation order."""

    # ── Final ────────────────────────────────────────────────────────
    final_action: Literal["BUY", "SELL", "HOLD", "VETOED"]
    token_usage: dict[str, int]

    # ── Degradation audit ────────────────────────────────────────────
    degraded_nodes: list[str]
    """Nodes whose LLM output was malformed and ran on a retry or a neutral
    fallback this pass. Recorded on the decision row so calibration /
    reflection can exclude degraded runs instead of learning from them."""
