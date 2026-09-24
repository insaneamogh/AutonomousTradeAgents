"""PLAN_PLATFORM Phase 5: route each thesis to the instrument its horizon fits.

One table for the rule, then one test per boundary it crosses: the
strategy_fit gate (and its flag), the Drafter's hold, and ghost grading.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from trading_agents.jobs.ghost_eval import _grading_horizon
from trading_agents.nodes import drafter as drafter_mod
from trading_agents.nodes import strategy_fit as strategy_fit_mod
from trading_agents.strategies.horizon import horizon_calendar_days
from trading_agents.strategies.router import route_instrument


@pytest.mark.parametrize(
    ("strategy", "direction", "requested", "shorts", "instrument", "reason"),
    [
        ("momentum", "long", "option", False, "equity", "horizon_60d_outlives_an_option"),
        ("sma_crossover", "long", "option", False, "equity", "horizon_20d_outlives_an_option"),
        ("unknown_strategy", "long", "option", False, "equity", "horizon_20d_outlives_an_option"),
        ("vol_regime_switch", "long", "option", False, "option", "horizon_15d_fits_an_option"),
        ("rsi_mean_reversion", "short", "option", False, "option", "horizon_5d_fits_an_option"),
        ("momentum", "short", "option", False, "option", "short_thesis_needs_a_put"),
        ("momentum", "short", "option", True, "equity", "horizon_60d_outlives_an_option"),
        ("rsi_mean_reversion", "long", "equity", False, "equity", "requested_equity"),
    ],
)
def test_route(strategy, direction, requested, shorts, instrument, reason) -> None:
    route = route_instrument(
        strategy_id=strategy, direction=direction, requested=requested,
        allow_equity_shorts=shorts,
    )
    assert (route.instrument, route.reason) == (instrument, reason)


_WINS_MOMENTUM_LONG = {
    "technicals": {"trend_regime": "uptrend", "dma20_pct": 2.0, "dma50_pct": 4.0,
                   "rsi_14": 55.0, "atr_14": 2.0, "volume_ratio_20d": 1.6},
    "quant": {"ret_252d_pct": 20.0, "ret_63d_pct": 10.0, "ret_21d_pct": 6.0, "sharpe": 1.0,
              "atr_zscore": 0.5, "realized_vol_pct": 25.0, "corr_benchmark": 0.5,
              "price_zscore_20": 0.5, "donchian_pct": 90.0},
}


async def test_the_router_takes_a_long_horizon_off_options_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.delenv("ALLOW_SHORTS", raising=False)
    state = {"symbol": "NVDA", "context": dict(_WINS_MOMENTUM_LONG),
             "instrument_preference": "option"}

    monkeypatch.delenv("INSTRUMENT_ROUTER_ENABLED", raising=False)
    off = await strategy_fit_mod.strategy_fit_node(dict(state))
    assert off["instrument"] == "option" and "instrument_route" not in off

    monkeypatch.setenv("INSTRUMENT_ROUTER_ENABLED", "1")
    on = await strategy_fit_mod.strategy_fit_node(dict(state))
    horizon = on["instrument_route"]["horizon_days"]
    assert horizon >= 20, on["selected_strategy"]
    assert "instrument" not in on  # equity
    assert on["strategy_fit"]["instrument_route"]["instrument"] == "equity"


async def test_a_routed_equity_thesis_is_held_for_its_own_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drafter_mod, "complete_json", AsyncMock(return_value=(
        {"verdict": "BUY", "confidence": 0.8, "rationale": "r", "bull_case": "b",
         "bear_case": "x", "risk_level": 3, "conviction_level": 3}, False)))
    state = {
        "symbol": "AAPL", "selected_strategy": "momentum", "selected_direction": "long",
        "selector_confidence": 0.7, "horizon": "short", "regime": "trending",
        "context": {"last_price": 250.0, "portfolio_equity": 100_000.0,
                    "technicals": {"atr_14": 4.0}},
        "technical": {"score": 60, "confidence": 0.6, "thesis": "t"},
    }
    plain = await drafter_mod.drafter_node(dict(state), llm=object())
    assert plain["proposal"]["time_stop_days"] == 5
    assert plain["proposal"]["horizon_trading_days"] is None

    routed = await drafter_mod.drafter_node(
        {**state, "instrument_route": {"instrument": "equity"}}, llm=object()
    )
    assert routed["proposal"]["time_stop_days"] == horizon_calendar_days("momentum")
    assert routed["proposal"]["horizon_trading_days"] == 60


@pytest.mark.parametrize(
    ("proposal", "label", "expected"),
    [
        ({"horizonTradingDays": 60}, "short", 60),
        ({"horizon_trading_days": 20}, "short", 20),
        ({"horizonTradingDays": None}, "short", 5),
        ({}, "long", 20),
        ({"horizonTradingDays": 999}, "short", 5),  # out of range: the label wins
    ],
)
def test_ghosts_grade_over_the_declared_horizon(proposal, label, expected) -> None:
    assert _grading_horizon(proposal, label) == expected
