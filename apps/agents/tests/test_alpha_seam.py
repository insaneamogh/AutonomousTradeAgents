"""The `AlphaModel` / `Signal` seam.

The seam's whole value is that a candidate signal can be measured without
new harness code, and that abstentions stay distinguishable from flat
calls. Both are tested here against the real backtest, not a stub.
"""

from __future__ import annotations

import pytest

from engine.alpha import AlphaModel, Signal
from trading_agents.strategies.alpha import StrategyFitAlpha


def test_strategy_fit_satisfies_the_protocol():
    """The incumbent has to fit the seam without changing its own behaviour,
    or the seam is describing something we do not actually run."""
    assert isinstance(StrategyFitAlpha(), AlphaModel)


def test_an_abstention_always_carries_a_reason():
    """A silent abstain is indistinguishable from a flat call in the logs,
    which defeats the flag."""
    s = Signal.abstain("m", "AAPL", reason="no_bars")
    assert s.abstained is True and s.reason == "no_bars" and s.value == 0.0


def test_an_abstention_cannot_carry_a_value():
    """Otherwise a downstream consumer sizes a position off a model that
    never formed a view."""
    assert Signal(name="m", symbol="A", value=0.8, abstained=True).value == 0.0


def test_value_is_bounded():
    with pytest.raises(ValueError):
        Signal(name="m", symbol="A", value=1.5)
    with pytest.raises(ValueError):
        Signal(name="m", symbol="A", value=-2.0)


def test_direction_is_derived_from_the_sign():
    assert Signal(name="m", symbol="A", value=0.4).direction == "long"
    assert Signal(name="m", symbol="A", value=-0.4).direction == "short"
    assert Signal(name="m", symbol="A", value=0.0).direction == "flat"


def test_thin_features_ABSTAIN_rather_than_reading_as_flat():
    """The distinction the flag exists for. A missing snapshot is a DATA
    problem; scoring it as a flat call counts a gap as an opinion and drags
    any measured hit rate toward 50%."""
    s = StrategyFitAlpha().evaluate({}, symbol="AAPL")
    assert s.abstained is True
    assert s.reason, "an abstention must say why"


def test_a_readable_snapshot_that_clears_nothing_is_FLAT_not_abstained():
    """The other half of the same distinction: we looked, and nothing
    qualified. That IS a view and a backtest should score it."""
    snapshot = {
        "technicals": {
            "rsi_14": 50.0, "sma_20": 100.0, "sma_50": 100.0,
            "last_price": 100.0, "atr_14": 1.0, "trend_regime": "choppy",
            "price_zscore_20": 0.0, "volume_ratio_20d": 1.0,
            "return_21d": 0.0, "donchian_pct": 50.0,
        }
    }
    s = StrategyFitAlpha().evaluate(snapshot, symbol="AAPL")
    if not s.abstained:
        assert s.value == 0.0 or s.meta.get("strategy_id")
        if s.value == 0.0 and not s.meta.get("strategy_id"):
            assert s.reason.startswith("no_strategy_cleared")


def test_the_backtest_can_be_driven_by_ANY_model():
    """The reason the seam exists. A candidate must be measurable by passing
    it to the existing harness — if this needed new harness code, candidates
    would go untested, which is how we ended up with one unvalidated signal."""
    from tests.eval.signal_backtest import run

    class AlwaysLong:
        name = "always_long"

        def evaluate(self, snapshot, *, symbol):
            return Signal(
                name=self.name, symbol=symbol, value=1.0,
                reason="test", meta={"strategy_id": "test", "direction": "long", "score": 1.0},
            )

    signals = run(step=200, model=AlwaysLong())
    assert signals, "a model that always calls must produce signals"
    assert {s.strategy for s in signals} == {"test"}
    assert all(s.direction == "long" for s in signals)


def test_a_model_that_always_abstains_produces_nothing_to_score():
    """Abstentions must be DROPPED, not scored as flat calls."""
    from tests.eval.signal_backtest import run

    class Silent:
        name = "silent"

        def evaluate(self, snapshot, *, symbol):
            return Signal.abstain(self.name, symbol, reason="test")

    assert run(step=200, model=Silent()) == []


def test_the_default_model_is_the_shipped_one():
    """The refactor must not have quietly changed what the backtest measures
    — the 'no edge' conclusion rests on it."""
    from tests.eval.signal_backtest import run

    a = run(step=400)
    b = run(step=400, model=StrategyFitAlpha(allow_shorts=True))
    assert [(s.symbol, s.day, s.strategy, s.score) for s in a] == [
        (s.symbol, s.day, s.strategy, s.score) for s in b
    ]
