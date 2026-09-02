"""``StrategyFit.conviction`` — the rank key, and why score could not be it.

The property under test is SEPARATION: conviction must distinguish setups
that the weighted mean calls identical. Everything else here is guarding
that one claim.
"""

from __future__ import annotations

import pytest

from trading_agents.strategies.fit import (
    NEUTRAL,
    FitComponent,
    _conviction,
    _weighted,
)


def _c(score: float, weight: float = 1.0, name: str = "x") -> FitComponent:
    return FitComponent(name=name, score=score, weight=weight, detail="")


def test_conviction_separates_two_setups_the_mean_calls_identical() -> None:
    """THE reason this field exists.

    A: nine components all barely above neutral — nothing actually agrees.
    B: four components fully convinced, five clearly against.

    Same weighted mean. Completely different setups: A is a shrug, B is a
    thesis with specific support and specific objections. Ranking B first
    is the whole point.
    """
    a = [_c(0.55) for _ in range(9)]
    b = [_c(1.0) for _ in range(4)] + [_c(0.19) for _ in range(5)]

    assert _weighted(a) == pytest.approx(_weighted(b), abs=0.01), (
        "precondition: the mean must be blind to the difference"
    )
    assert _conviction(b) > _conviction(a) * 2, (
        "conviction must see what the mean cannot"
    )


def test_evidence_at_or_below_neutral_contributes_nothing() -> None:
    """A neutral or negative check is not support. Counting it as partial
    support is how a setup with no evidence scores like one with some."""
    assert _conviction([_c(NEUTRAL) for _ in range(5)]) == 0.0
    assert _conviction([_c(0.0) for _ in range(5)]) == 0.0
    assert _conviction([_c(0.3), _c(0.5)]) == 0.0


def test_conviction_is_continuous_not_a_step() -> None:
    """The first version counted components over a 0.6 line, which with ~9
    components can only take ~9 values — measured at TWO distinct values
    across a hundred scenarios, coarser than the number it replaced. A
    stronger component must register as stronger."""
    weak = _conviction([_c(0.6), _c(0.6)])
    mid = _conviction([_c(0.8), _c(0.8)])
    strong = _conviction([_c(1.0), _c(1.0)])
    assert 0.0 < weak < mid < strong == pytest.approx(1.0)


def test_weight_is_respected() -> None:
    """A heavily-weighted check that agrees should count for more than a
    light one, exactly as it does in the mean."""
    heavy_agrees = _conviction([_c(1.0, weight=9.0), _c(0.0, weight=1.0)])
    light_agrees = _conviction([_c(1.0, weight=1.0), _c(0.0, weight=9.0)])
    assert heavy_agrees > light_agrees


def test_degenerate_inputs_do_not_raise() -> None:
    assert _conviction([]) == 0.0
    assert _conviction([_c(1.0, weight=0.0)]) == 0.0


def test_conviction_stays_in_the_unit_interval() -> None:
    for comps in ([_c(1.0)], [_c(1.0) for _ in range(20)], [_c(0.0)], []):
        assert 0.0 <= _conviction(comps) <= 1.0


def test_the_rank_key_orders_by_conviction_before_score() -> None:
    from trading_agents.strategies.fit import StrategyFit

    def _fit(conviction: float, score: float) -> StrategyFit:
        return StrategyFit(
            strategy_id="s", direction="long", fit=score, prior=0.5,
            prior_multiplier=1.0, score=score, reason="", summary="",
            conviction=conviction,
        )

    # Lower score but higher conviction must rank FIRST — that inversion
    # is the entire behavioural change, so it is asserted directly.
    low_score_high_conviction = _fit(conviction=0.9, score=0.61)
    high_score_low_conviction = _fit(conviction=0.2, score=0.99)
    ordered = sorted(
        [high_score_low_conviction, low_score_high_conviction],
        key=lambda f: f.rank_key,
    )
    assert ordered[0] is low_score_high_conviction

    # Score still breaks ties, keeping the ordering total.
    a, b = _fit(0.5, 0.70), _fit(0.5, 0.90)
    assert sorted([a, b], key=lambda f: f.rank_key)[0] is b


def test_conviction_does_not_change_what_is_tradable() -> None:
    """Deliberately NOT wired into the pass/fail gate. This change reorders
    which candidates get attention first; it must not alter which are
    allowed to trade at all, or a ranking tweak becomes a risk change."""
    from trading_agents.strategies.fit import MIN_FIT_TO_TRADE, StrategyFit

    fit = StrategyFit(
        strategy_id="s", direction="long", fit=0.9, prior=0.5,
        prior_multiplier=1.0, score=0.9, reason="", summary="",
        conviction=0.0,
    )
    assert fit.tradable is (0.9 >= MIN_FIT_TO_TRADE)
