"""expected_move_below_breakeven: a right call with an ordinary move must
not lose. That is the shape of GILD155 (-23.5%), GILD150 (-13.8%) and AMD
(-10.1%) in the recorded book: direction right, move too small."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from engine.options.breakeven import TYPICAL_ABS_MOVE_FACTOR, expected_move_pct, required_move_pct
from engine.options.pricing import price, strike_for_delta
from engine.options.rules import expected_move_below_breakeven
from engine.risk import RiskCaps, evaluate
from engine.risk.types import OptionLegDetails, RiskContext, RiskProposal, Side

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


def _leg(*, spot: float, iv: float, rv: float | None, target_delta: float = 0.45,
         dte: int = 30, kind: str = "call", half_spread: float = 0.02) -> OptionLegDetails:
    t = dte / 365.0
    strike = round(strike_for_delta(spot, t, iv, kind=kind, target_abs_delta=target_delta), 2)  # type: ignore[arg-type]
    mid = price(spot, strike, t, iv, kind=kind)  # type: ignore[arg-type]
    return OptionLegDetails(
        underlying_symbol="XYZ", occ_symbol="XYZ261001C00100000", contract_type=kind,  # type: ignore[arg-type]
        strike=strike, expiry=date(2026, 10, 1), open_interest=5000, volume=50,
        bid=round(mid * (1 - half_spread), 2), ask=round(mid * (1 + half_spread), 2),
        implied_volatility=iv, delta=target_delta,
        underlying_price=spot, underlying_realized_vol_pct=rv,
    )


def _proposal(leg: OptionLegDetails) -> RiskProposal:
    return RiskProposal(
        symbol="XYZ", side=Side.BUY, qty=1, estimated_notional=(leg.ask or 1.0) * 100,
        last_price=leg.ask or 1.0, confidence=0.55, is_option=True, option=leg,
    )


def _ctx() -> RiskContext:
    return RiskContext(account_equity=100_000.0, cash=100_000.0, buying_power=100_000.0,
                       options_trading_level=3, now_utc=NOW)


# ── the pure math ────────────────────────────────────────────────────


def test_expected_move_is_the_mean_absolute_move() -> None:
    assert expected_move_pct(realized_vol_pct=40.0, hold_days=5) == pytest.approx(
        40.0 * (5 / 365) ** 0.5 * TYPICAL_ABS_MOVE_FACTOR
    )


def test_breakeven_is_further_away_for_a_far_otm_contract() -> None:
    near = _leg(spot=100, iv=0.35, rv=30, target_delta=0.50)
    far = _leg(spot=100, iv=0.35, rv=30, target_delta=0.20)
    args = dict(spot=100.0, kind="call", dte_days=30, iv=0.35, half_spread_pct=2.0, hold_days=5)
    r_near = required_move_pct(strike=near.strike, paid=near.ask, **args)  # type: ignore[arg-type]
    r_far = required_move_pct(strike=far.strike, paid=far.ask, **args)  # type: ignore[arg-type]
    assert r_near is not None and r_far is not None and r_far > r_near > 0


def test_a_put_breaks_even_on_a_move_down() -> None:
    leg = _leg(spot=100, iv=0.35, rv=30, kind="put")
    r = required_move_pct(spot=100.0, strike=leg.strike, kind="put", dte_days=30, iv=0.35,
                          paid=leg.ask, half_spread_pct=2.0, hold_days=5)
    assert r is not None and 0 < r < 10


def test_a_contract_that_dies_inside_the_hold_cannot_break_even_on_time() -> None:
    """3 DTE, 5-day hold, far OTM: no move within 3x recovers it, so None."""
    assert required_move_pct(spot=100.0, strike=300.0, kind="call", dte_days=3, iv=0.3,
                             paid=1.0, half_spread_pct=2.0, hold_days=5) is None


# ── the rule ─────────────────────────────────────────────────────────


def test_a_volatile_name_near_the_money_passes() -> None:
    """NVDA-like: IV 45%, realized 40%. The breakeven is well inside the
    typical 5-day move."""
    leg = _leg(spot=180, iv=0.45, rv=40.0)
    assert expected_move_below_breakeven(_proposal(leg), _ctx(), RiskCaps()) is None


def test_a_quiet_name_with_rich_iv_is_refused_by_name() -> None:
    """GILD-like: realized 12% but IV priced at 30%, 0.25 delta. Even a right
    call with an ordinary move loses to theta and the spread."""
    leg = _leg(spot=100, iv=0.30, rv=12.0, target_delta=0.25)
    d = expected_move_below_breakeven(_proposal(leg), _ctx(), RiskCaps())
    assert d is not None and not d.approved
    assert d.veto_rule == "expected_move_below_breakeven"


@pytest.mark.parametrize("field", ["underlying_price", "underlying_realized_vol_pct",
                                   "implied_volatility", "ask"])
def test_missing_inputs_self_gate_rather_than_refuse(field: str) -> None:
    leg = replace(_leg(spot=100, iv=0.30, rv=12.0, target_delta=0.25), **{field: None})
    assert expected_move_below_breakeven(_proposal(leg), _ctx(), RiskCaps()) is None


def test_the_ratio_can_disable_the_rule() -> None:
    leg = _leg(spot=100, iv=0.30, rv=12.0, target_delta=0.25)
    caps = RiskCaps(options_breakeven_move_ratio=None)
    assert expected_move_below_breakeven(_proposal(leg), _ctx(), caps) is None


def test_the_rule_is_in_the_live_options_sequence() -> None:
    """Through the TOP-LEVEL evaluate(), not the rule function. A rule that
    passes its unit tests but is never called is the NameError-in-
    option_stops failure shape."""
    leg = _leg(spot=100, iv=0.30, rv=12.0, target_delta=0.25)
    decision = evaluate(_proposal(leg), _ctx(), RiskCaps(options_disabled=False))
    assert not decision.approved
    assert decision.veto_rule == "expected_move_below_breakeven"
