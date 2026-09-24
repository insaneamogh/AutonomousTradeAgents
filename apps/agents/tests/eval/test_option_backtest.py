"""The options-aware backtest: premium P&L, not direction.

Measured 2026-09-23 over the 6-year fixture (|delta| 0.45, IV = RV20 x 1.15,
2.5%/side, -40% stop):

    horizon   shipped signal              perfect-direction oracle
       2d     -8.2%/trade  t=-12.4  hit 32%      +37.3%
       5d     -7.0%        t= -6.3  hit 26%      +41.4%
      10d     -5.4%        t= -3.4  hit 22%      +42.6%
      20d     -5.8%        t= -2.5  hit 18%      +38.0%

The oracle column is the point. The option vehicle is NOT the problem: a
correct direction call keeps +37-43% per trade after theta and spread. The
book lost because the signal is a coin flip, and a coin flip bought through
premium pays theta and spread on every flip.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from tests.eval.acceptance import evaluate
from tests.eval.option_backtest import (
    OPTION_HORIZONS,
    OPTIONS_BAR,
    OptionModel,
    SpreadModel,
    backtest,
    dte_for,
    simulate,
    simulate_spread,
)
from tests.eval.signal_backtest import _load, run

from engine.features.technicals import DailyBar


def _bars(closes: list[float]) -> list[DailyBar]:
    out, d = [], date(2024, 1, 2)
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(DailyBar(day=d, open=c, high=c * 1.005, low=c * 0.995, close=c, volume=1e6))
        d += timedelta(days=1)
    return out


def _noisy(n: int, base: float = 100.0) -> list[float]:
    """Deterministic +/-1% zig-zag: ~16% annualised realized vol, no drift."""
    return [base * (1.01 if i % 2 else 0.99) for i in range(n)]


def test_a_right_call_with_no_move_still_loses() -> None:
    """entry_quality's finding, reproduced by the model: direction right,
    move too small, and theta plus spread take it."""
    closes = _noisy(30) + [100.0] * 6
    bars = _bars(closes)
    ret = simulate(bars, 29, "long", 5, OptionModel())
    assert ret is not None and ret < 0


def test_a_right_call_with_a_real_move_wins() -> None:
    closes = _noisy(30) + [100.0 * (1.02 ** k) for k in range(1, 7)]
    ret = simulate(_bars(closes), 29, "long", 5, OptionModel())
    assert ret is not None and ret > 30


def test_the_stop_caps_a_wrong_call_near_its_level() -> None:
    closes = _noisy(30) + [100.0 * (0.97 ** k) for k in range(1, 7)]
    ret = simulate(_bars(closes), 29, "long", 5, OptionModel())
    assert ret is not None and -70 < ret <= -40


def test_a_hold_longer_than_any_contract_is_refused() -> None:
    """60 trading days is ~84 calendar days, past the 45-DTE ceiling."""
    assert dte_for(60, OptionModel()) is None
    assert dte_for(5, OptionModel()) == 14


@pytest.fixture(scope="module")
def fixture_run():
    return _load(), run(step=10)


def test_the_option_vehicle_is_not_the_problem(fixture_run) -> None:
    """A perfect direction call must survive theta and spread by a wide
    margin, or the model is broken, or options are untradeable for anyone."""
    data, signals = fixture_run
    for h in OPTION_HORIZONS:
        obs = backtest(h, signals=signals, data=data, oracle=True)
        mean = sum(o.ret_pct for o in obs) / len(obs)
        assert mean > 20, f"oracle at {h}d only {mean:+.1f}%"


def test_the_shipped_signal_fails_the_options_bar_at_every_horizon(fixture_run) -> None:
    data, signals = fixture_run
    for h in OPTION_HORIZONS:
        verdict = evaluate(
            backtest(h, signals=signals, data=data), OPTIONS_BAR, n_tests=len(OPTION_HORIZONS)
        )
        assert not verdict.passed, f"{h}d unexpectedly passed: {verdict.line()}"
        assert verdict.mean_net_pct < 0


def test_a_debit_spread_is_capped_at_its_width() -> None:
    """A huge right move: the single leg keeps running, the spread cannot
    pay more than (width - debit) / debit however far the stock goes."""
    closes = _noisy(30) + [100.0 * (1.05 ** k) for k in range(1, 7)]
    bars = _bars(closes)
    single = simulate(bars, 29, "long", 5, OptionModel())
    vertical = simulate_spread(bars, 29, "long", 5, SpreadModel())
    assert single is not None and vertical is not None
    assert 0 < vertical < single


def test_a_put_spread_mirrors_a_call_spread() -> None:
    up = _bars(_noisy(30) + [100.0 * (1.02 ** k) for k in range(1, 7)])
    down = _bars(_noisy(30) + [100.0 * (0.98 ** k) for k in range(1, 7)])
    call = simulate_spread(up, 29, "long", 5, SpreadModel())
    put = simulate_spread(down, 29, "short", 5, SpreadModel())
    assert call is not None and put is not None
    assert call > 0 and put > 0


def test_the_spread_is_not_cheaper_for_a_perfect_call(fixture_run) -> None:
    """The measured reason debit spreads are NOT the Phase 5 default
    (2026-09-25): with the live 2.5%/side cost on each leg, the oracle
    keeps less through a spread than through the single leg."""
    data, signals = fixture_run
    single = backtest(5, signals=signals, data=data, oracle=True)
    vertical = backtest(5, signals=signals, data=data, oracle=True, spread=SpreadModel())
    mean = lambda obs: sum(o.ret_pct for o in obs) / len(obs)  # noqa: E731
    assert mean(vertical) < mean(single)
