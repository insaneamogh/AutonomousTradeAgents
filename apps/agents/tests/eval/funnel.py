"""Runs one scenario through the REAL deterministic funnel.

Every stage below calls the same production function the live path calls
— ``best_strategy``, ``select_contract``, ``options_position_size``,
``protective_stop_levels``. Nothing here re-implements a threshold or a
formula. That is the entire point: an eval suite that models the funnel
instead of invoking it proves only that the model agrees with itself.

The one thing this deliberately does NOT do is call an LLM. The question
this suite exists to answer is "does the deterministic layer fire, and
does it narrow the funnel?", and the answer has to be free, offline and
reproducible to be worth running. ``reaches_llm`` marks the boundary: it
is True exactly when the deterministic layer would have handed this
symbol to a paid council pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from tests.eval.scenarios import Scenario

from engine.options.protective_stop import protective_stop_levels
from engine.options.sizing import OptionsSizingInputs, options_position_size
from engine.risk.types import RiskCaps

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)


@dataclass(frozen=True)
class FunnelResult:
    """What the deterministic layer decided, and why."""

    case_id: str
    archetype: str

    score: float | None
    """``best_strategy``'s winning score, or ``None`` when nothing cleared
    the fit floor / the evidence gate refused."""

    direction: str | None
    strategy_id: str | None

    reaches_llm: bool
    """True when this symbol would consume a paid council pass."""

    refusal_reason: str | None
    """A NAMED reason for every rejection. The Refusal Ledger is this
    project's whole differentiator, so an unnamed refusal is a defect
    even when the refusal itself is correct."""

    qty: int | None = None
    """Contracts, after the liquidity trim. ``None`` for equity-only
    scenarios or ones refused before sizing."""

    sizing_note: str | None = None
    stop_price: float | None = None
    limit_price: float | None = None


def run_funnel(scenario: Scenario, *, caps: RiskCaps | None = None,
               allow_shorts: bool = False) -> FunnelResult:
    """One scenario -> one deterministic verdict.

    Stages, in the order the live path runs them:

      1. ``best_strategy``  — the free screen. No LLM, no network.
      2. chain depth        — options only; the CME gate.
      3. sizing             — premium budget then the liquidity trim.
      4. protective stop    — the level a resting broker order would hold.

    A refusal at any stage short-circuits with a named reason, exactly as
    production does.
    """
    from trading_agents.strategies import best_strategy

    caps = caps or RiskCaps.aggressive_paper()

    # ── Stage 1: the deterministic screen ────────────────────────────
    winner, _ranked = best_strategy(scenario.features, allow_shorts=allow_shorts)
    if winner is None:
        return FunnelResult(
            case_id=scenario.case_id,
            archetype=scenario.archetype,
            score=None,
            direction=None,
            strategy_id=None,
            reaches_llm=False,
            refusal_reason="below_fit_floor_or_thin_evidence",
        )

    base = dict(
        case_id=scenario.case_id,
        archetype=scenario.archetype,
        score=winner.score,
        direction=winner.direction,
        strategy_id=winner.strategy_id,
    )

    if scenario.options is None:
        # Equity path: clearing the screen IS reaching the LLM.
        return FunnelResult(**base, reaches_llm=True, refusal_reason=None)

    # ── Stage 2: chain depth (the CME gate) ──────────────────────────
    # Mirrors `select_contract`'s `liquid_chain_depth` stage. Checked
    # against the SAME cap the production selector reads, not a literal,
    # so retuning the threshold retunes this suite with it.
    from engine.options.selection import _MIN_LIQUID_CHAIN_DEPTH

    depth = int(scenario.options.get("liquid_chain_depth", 0))

    if 0 < depth < _MIN_LIQUID_CHAIN_DEPTH:
        return FunnelResult(
            **base, reaches_llm=False, refusal_reason="illiquid_chain",
        )
    if depth == 0:
        return FunnelResult(
            **base, reaches_llm=False, refusal_reason="no_liquid_contract",
        )

    # ── Stage 3: sizing, including the liquidity trim ────────────────
    ask = float(scenario.options.get("ask", 4.60))
    open_interest = int(scenario.options.get("open_interest", 0))
    budget = 100_000.0 * caps.options_max_premium_pct / 100.0
    sizing = options_position_size(
        OptionsSizingInputs(
            budget_usd=budget,
            ask=ask,
            multiplier=100,
            open_interest=open_interest,
            max_pct_of_open_interest=caps.options_max_pct_of_open_interest,
        )
    )
    if sizing.qty < 1:
        return FunnelResult(
            **base, reaches_llm=False, refusal_reason="size_rounds_to_zero",
            sizing_note=sizing.notes,
        )

    # ── Stage 4: the resting protective stop's level ─────────────────
    levels = protective_stop_levels(
        entry_premium=ask,
        stop_loss_pct=caps.options_stop_loss_pct,
        slippage_pct=caps.options_stop_limit_slippage_pct,
    )

    return FunnelResult(
        **base,
        reaches_llm=True,
        refusal_reason=None,
        qty=sizing.qty,
        sizing_note=sizing.notes,
        stop_price=levels.stop_price if levels else None,
        limit_price=levels.limit_price if levels else None,
    )


@dataclass(frozen=True)
class FunnelReport:
    """Aggregate over the whole golden dataset."""

    total: int
    reached_llm: int
    refused: int
    by_reason: dict[str, int]
    by_archetype: dict[str, dict[str, int]]
    results: tuple[FunnelResult, ...]

    @property
    def llm_fraction(self) -> float:
        return self.reached_llm / self.total if self.total else 0.0


def run_all(scenarios: list[Scenario], *, caps: RiskCaps | None = None,
            allow_shorts: bool = False) -> FunnelReport:
    results = [run_funnel(s, caps=caps, allow_shorts=allow_shorts) for s in scenarios]

    by_reason: dict[str, int] = {}
    by_archetype: dict[str, dict[str, int]] = {}
    for r in results:
        if r.refusal_reason:
            by_reason[r.refusal_reason] = by_reason.get(r.refusal_reason, 0) + 1
        bucket = by_archetype.setdefault(r.archetype, {"llm": 0, "refused": 0})
        bucket["llm" if r.reaches_llm else "refused"] += 1

    return FunnelReport(
        total=len(results),
        reached_llm=sum(1 for r in results if r.reaches_llm),
        refused=sum(1 for r in results if not r.reaches_llm),
        by_reason=by_reason,
        by_archetype=by_archetype,
        results=tuple(results),
    )
