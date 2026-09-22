"""The acceptance bar: drawn before looking, as code."""

from __future__ import annotations

import random
from datetime import date, timedelta

from tests.eval.acceptance import AcceptanceBar, Observation, evaluate, required_t


def _obs(means: list[float], *, sd: float = 2.0, seed: int = 7, years: float = 6.0):
    """len(means) observations spread evenly over `years`, each drawn around
    its own mean. Deterministic."""
    rng = random.Random(seed)
    n = len(means)
    start = date(2020, 1, 1)
    step = years * 365.25 / n
    return [
        Observation(day=start + timedelta(days=int(i * step)), ret_pct=rng.gauss(m, sd))
        for i, m in enumerate(means)
    ]


def test_a_real_stable_edge_passes() -> None:
    v = evaluate(_obs([0.6] * 600))
    assert v.passed, v.line()


def test_a_coin_flip_fails() -> None:
    v = evaluate(_obs([0.0] * 600))
    assert not v.passed
    assert "not_significant" in v.failures


def test_an_edge_smaller_than_costs_fails() -> None:
    """Right before costs, wrong after them, is wrong."""
    v = evaluate(_obs([0.05] * 5000, sd=0.2), AcceptanceBar(cost_pct=0.10))
    assert not v.passed
    assert "negative_after_costs" in v.failures


def test_an_edge_confined_to_one_regime_fails() -> None:
    v = evaluate(_obs([1.2] * 300 + [-0.1] * 300))
    assert not v.passed
    assert "unstable_across_halves" in v.failures


def test_too_few_and_too_short_are_named() -> None:
    v = evaluate(_obs([2.0] * 40, years=2.0))
    assert "too_few_observations" in v.failures
    assert "history_too_short" in v.failures


def test_the_threshold_rises_with_the_number_of_tests() -> None:
    """Five horizons at 5% each gives ~23% odds that one crosses by chance."""
    bar = AcceptanceBar()
    assert required_t(bar, 1) == bar.min_t
    assert required_t(bar, 5) > 2.5


def test_no_observations_fails_rather_than_raising() -> None:
    v = evaluate([])
    assert not v.passed and v.failures == ("no_observations",)
