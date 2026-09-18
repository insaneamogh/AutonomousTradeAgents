"""Thesis horizon vs contract life.

The largest defect measured in this system: the signals predict months and
the positions were held **2.1 days**. `_momentum` is 12-1 momentum built on
252-day and 63-day trailing returns; `_sma_crossover` is 20/50-day moving
averages. Neither can resolve in two days, and 85% of 1,427 strategy
selections were trend strategies of that kind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.options.rules import horizon_exceeds_contract
from engine.risk.types import (
    OptionLegDetails,
    RiskCaps,
    RiskContext,
    RiskProposal,
    Side,
)
from trading_agents.strategies.horizon import (
    horizon_calendar_days,
    is_horizon_consistent,
    strategy_horizon_days,
)

_NOW = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)


def _prop(strategy: str | None, dte_days: int, action: str = "buy_to_open"):
    opt = OptionLegDetails(
        underlying_symbol="NVDA", occ_symbol="NVDA261016C00225000",
        contract_type="call", strike=225.0,
        expiry=_NOW.date() + timedelta(days=dte_days),
        multiplier=100, action=action,
    )
    return RiskProposal(
        symbol=opt.occ_symbol, side=Side.BUY, qty=1, last_price=2.50,
        estimated_notional=250.0, confidence=0.7, is_option=True,
        option=opt, strategy_id=strategy,
    )


def _ctx() -> RiskContext:
    return RiskContext(
        account_equity=100_000.0, cash=100_000.0, buying_power=100_000.0,
        options_trading_level=3, now_utc=_NOW,
    )


def _fire(strategy: str | None, dte: int, **kw):
    return horizon_exceeds_contract(_prop(strategy, dte, **kw), _ctx(), RiskCaps())


# ── the horizon table ─────────────────────────────────────────────────


def test_each_horizon_matches_the_strategys_own_window() -> None:
    """Not preferences — the scorer's own measurement window. A signal from
    a 50-day moving average cannot resolve in two days; one from RSI-14
    extremes is not a three-month thesis."""
    assert strategy_horizon_days("momentum") == 60          # 252d/63d returns
    assert strategy_horizon_days("sma_crossover") == 20     # 20/50 DMA
    assert strategy_horizon_days("breakout") == 20          # Donchian 20
    assert strategy_horizon_days("rsi_mean_reversion") == 5  # RSI-14 snapback


def test_an_unknown_strategy_does_not_default_to_the_shortest() -> None:
    """Defaulting to days is how the current mismatch would silently
    reappear for any strategy added later."""
    assert strategy_horizon_days("brand_new") > strategy_horizon_days(
        "rsi_mean_reversion"
    )
    assert strategy_horizon_days(None) == strategy_horizon_days("brand_new")


def test_trading_days_are_converted_to_calendar_days() -> None:
    """The units genuinely differ. Comparing a trading-day horizon straight
    against an expiry date understates the contract a thesis needs by a
    fifth — 60 trading days is ~84 calendar days, not 60."""
    assert horizon_calendar_days("momentum") == 84
    assert horizon_calendar_days("momentum") > strategy_horizon_days("momentum")


def test_momentum_has_no_tradeable_contract_under_the_current_dte_cap() -> None:
    """Worth failing loudly if it ever changes: a momentum thesis needs ~84
    calendar days and `options_max_dte` is 60, so 23% of selections
    (322 of 1,427) are structurally un-tradeable at their own horizon.
    Either the cap moves or that thesis belongs in equity."""
    assert horizon_calendar_days("momentum") > RiskCaps().options_max_dte


# ── the rule ──────────────────────────────────────────────────────────


def test_a_months_long_thesis_in_a_30_day_option_is_refused() -> None:
    d = _fire("momentum", 30)
    assert d is not None
    assert d.veto_rule == "horizon_exceeds_contract"
    assert "momentum" in d.reason


def test_a_long_enough_contract_passes() -> None:
    assert _fire("momentum", 90) is None


def test_a_short_horizon_thesis_is_fine_in_a_short_contract() -> None:
    """The rule must not simply prefer long-dated contracts — an RSI
    snapback in a 10-day option is perfectly coherent."""
    assert _fire("rsi_mean_reversion", 10) is None


def test_it_self_gates_when_it_cannot_assess() -> None:
    assert _fire(None, 30) is None, "unattributed is not a veto"
    assert _fire("momentum", 30, action="sell_to_close") is None, "exits are exempt"


@pytest.mark.parametrize("dte", [83, 84])
def test_the_boundary_is_inclusive(dte: int) -> None:
    """84 calendar days is exactly enough; 83 is not."""
    assert (_fire("momentum", dte) is None) is (dte >= 84)


# ── the helper ────────────────────────────────────────────────────────


def test_is_horizon_consistent_agrees_with_the_rule() -> None:
    assert is_horizon_consistent(strategy_id="momentum", dte=90)
    assert not is_horizon_consistent(strategy_id="momentum", dte=30)
    assert is_horizon_consistent(strategy_id="momentum", dte=None), (
        "unknown DTE is not a veto — min_dte refuses a missing expiry by name"
    )


# ── the wiring, not just the rule ─────────────────────────────────────


def test_the_rule_is_actually_IN_the_options_sequence() -> None:
    """Every test above calls `horizon_exceeds_contract` directly, so they
    all still pass if the rule is never wired into `evaluate_option` — the
    exact gap that let a NameError sit undetected in `option_stops` until
    ruff caught it. This one goes through the top-level `evaluate()`, so it
    fails if the rule is unwired.
    """
    from engine.risk import evaluate

    caps = RiskCaps(
        options_disabled=False,
        min_council_confidence=0.30,
        options_max_dte=120,        # let the contract itself through
        options_min_open_interest=0,
        options_min_volume=0,
    )
    opt = OptionLegDetails(
        underlying_symbol="NVDA", occ_symbol="NVDA261016C00225000",
        contract_type="call", strike=225.0,
        expiry=_NOW.date() + timedelta(days=30),
        multiplier=100, action="buy_to_open",
        open_interest=500, volume=100, bid=2.45, ask=2.55,
        implied_volatility=0.28,
    )
    proposal = RiskProposal(
        symbol=opt.occ_symbol, side=Side.BUY, qty=1, last_price=2.50,
        estimated_notional=250.0, confidence=0.70, is_option=True,
        option=opt, strategy_id="momentum",
    )
    d = evaluate(proposal, _ctx(), caps)
    assert not d.approved
    assert d.veto_rule == "horizon_exceeds_contract", (
        f"refused by {d.veto_rule} instead — the horizon rule is either "
        "unwired or ordered behind another rule that fires first"
    )
