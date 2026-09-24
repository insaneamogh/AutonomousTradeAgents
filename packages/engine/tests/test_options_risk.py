"""Options risk-pipeline tests — engine.options.risk.evaluate_option, run
through the TOP-LEVEL engine.risk.evaluate() so the ``proposal.is_option``
dispatch itself stays covered, mirroring test_risk.py's own convention of
exercising rules through ``evaluate()`` rather than calling rule functions
in isolation.

Every fixture below is built to be "otherwise compliant" — passing every
rule EXCEPT the one a given test mutates — so a veto can only be
attributed to the rule under test. ``now_utc`` is always injected (never
the real wall clock), matching ``engine.risk.rules.mis_square_off``'s own
now-injection convention that ``engine.options.expiry`` reuses.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from engine.options.contracts import to_risk_proposal
from engine.risk import (
    OptionLegDetails,
    PortfolioPosition,
    RiskCaps,
    RiskContext,
    RiskProposal,
    Side,
    evaluate,
)
from engine.risk.markets import is_derivative, market_of

_NOW = datetime(2026, 1, 10, 15, 0, tzinfo=UTC)
_OCC = "AAPL260201C00250000"


def _ctx(**overrides: object) -> RiskContext:
    base: dict[str, object] = dict(
        account_equity=100_000.0,
        cash=100_000.0,
        buying_power=100_000.0,
        options_trading_level=3,
        now_utc=_NOW,
    )
    base.update(overrides)
    return RiskContext(**base)  # type: ignore[arg-type]


def _option(**overrides: object) -> OptionLegDetails:
    base: dict[str, object] = dict(
        underlying_symbol="AAPL",
        occ_symbol=_OCC,
        contract_type="call",
        strike=250.0,
        expiry=date(2026, 2, 1),  # 22 days from _NOW — safely inside [7, 60] DTE
        multiplier=100,
        action="buy_to_open",
        open_interest=500,
        volume=100,
        bid=2.45,
        ask=2.55,  # relative spread = 0.10/2.50*100 = 4.0%, under the 8% cap
        implied_volatility=0.28,
        days_to_earnings=None,  # self-gates earnings_blackout
    )
    base.update(overrides)
    return OptionLegDetails(**base)  # type: ignore[arg-type]


def _entry(
    *, qty: int = 1, last_price: float = 2.50, option: OptionLegDetails | None = None,
    **overrides: object,
) -> RiskProposal:
    opt = option or _option()
    return to_risk_proposal(
        symbol=opt.occ_symbol,
        side=Side.BUY,
        qty=qty,
        estimated_notional=qty * last_price * opt.multiplier,
        last_price=last_price,
        confidence=overrides.pop("confidence", 0.70),  # type: ignore[arg-type]
        option=opt,
        **overrides,  # type: ignore[arg-type]
    )


def _close(
    *, qty: int = 1, last_price: float = 2.50, option: OptionLegDetails | None = None,
    **overrides: object,
) -> RiskProposal:
    opt = option or _option(action="sell_to_close")
    return to_risk_proposal(
        symbol=opt.occ_symbol,
        side=Side.SELL,
        qty=qty,
        estimated_notional=qty * last_price * opt.multiplier,
        last_price=last_price,
        confidence=1.0,
        option=opt,
        **overrides,  # type: ignore[arg-type]
    )


ENABLED = RiskCaps(options_disabled=False)


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────


def test_happy_entry_clears_every_rule() -> None:
    # min_specialist_avg_score is deliberately NOT in this list: no options
    # entry point ever passes `specialists=` (technical/fundamental/macro
    # never run ahead of the Bull/Bear council), so the rule always
    # self-gates here — see test_specialist_avg_score_self_gates_on_every_
    # options_entry below for the dedicated coverage of that fact.
    d = evaluate(_entry(), _ctx(), ENABLED)
    assert d.approved
    assert d.veto_rule is None
    for name in (
        "options_disabled",
        "naked_short_forbidden",
        "options_level_insufficient",
        "expiry_day_entry",
        "min_dte",
        "max_dte",
        "illiquid_contract",
        "iv_unavailable",
        "earnings_blackout",
        "min_council_confidence",
        "pdt_block",
        "max_open_positions",
        "max_premium_pct",
        "max_total_premium_pct",
    ):
        assert name in d.checks_passed, f"{name} missing from {d.checks_passed}"


def test_specialist_avg_score_self_gates_on_every_options_entry() -> None:
    # No caller on the options path ever supplies `specialists=` (default
    # `()`) — this asserts the self-gate is honored correctly: the rule
    # neither vetoes nor claims to have run. Confirms the fix for the bug
    # where both call sites unconditionally recorded this rule as passed
    # even when it never ran at all.
    d = evaluate(_entry(), _ctx(), ENABLED)
    assert d.approved
    assert "min_specialist_avg_score" not in d.checks_passed

    # And still correctly enforces the floor on the rare/future path where
    # real specialists ARE supplied (e.g. via executor.py's _re_run_risk),
    # proving the self-gate fix didn't quietly break the rule itself.
    from engine.risk import SpecialistScore

    blocked = evaluate(
        _entry(),
        _ctx(),
        ENABLED,
        specialists=(SpecialistScore("technical", 20.0, 0.4),),
    )
    assert not blocked.approved
    assert blocked.veto_rule == "min_specialist_avg_score"


# ─────────────────────────────────────────────────────────────────────
# options_disabled
# ─────────────────────────────────────────────────────────────────────


def test_options_disabled_never_blocks_a_close() -> None:
    d = evaluate(_close(), _ctx(), RiskCaps())  # still disabled — must not matter
    assert d.approved


# ─────────────────────────────────────────────────────────────────────
# naked_short_forbidden
# ─────────────────────────────────────────────────────────────────────


def test_naked_short_forbidden_blocks_any_other_action() -> None:
    bad_option = _option(action="sell_to_open")
    d = evaluate(_entry(option=bad_option), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "naked_short_forbidden"


# ─────────────────────────────────────────────────────────────────────
# options_level_insufficient
# ─────────────────────────────────────────────────────────────────────


def test_options_level_insufficient_at_level_2_passes() -> None:
    d = evaluate(_entry(), _ctx(options_trading_level=2), ENABLED)
    assert d.veto_rule != "options_level_insufficient"


def test_options_level_insufficient_when_level_unknown() -> None:
    d = evaluate(_entry(), _ctx(options_trading_level=None), ENABLED)
    assert not d.approved
    assert d.veto_rule == "options_level_insufficient"


# ─────────────────────────────────────────────────────────────────────
# expiry_day_entry
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# min_dte / max_dte — boundary values
# ─────────────────────────────────────────────────────────────────────


def test_min_dte_blocks_one_day_under_the_floor() -> None:
    opt = _option(expiry=date(2026, 1, 16))  # 6 days from _NOW — cap is 7
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "min_dte"


def test_min_dte_passes_at_exactly_the_floor() -> None:
    opt = _option(expiry=date(2026, 1, 17))  # exactly 7 days from _NOW
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert d.veto_rule != "min_dte"


def test_max_dte_blocks_one_day_over_the_ceiling() -> None:
    opt = _option(expiry=date(2026, 3, 12))  # 61 days from _NOW — cap is 60
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "max_dte"


def test_max_dte_passes_at_exactly_the_ceiling() -> None:
    opt = _option(expiry=date(2026, 3, 11))  # exactly 60 days from _NOW
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert d.veto_rule != "max_dte"


# ─────────────────────────────────────────────────────────────────────
# illiquid_contract — three independent sub-conditions
# ─────────────────────────────────────────────────────────────────────


def test_illiquid_contract_blocks_missing_open_interest() -> None:
    opt = _option(open_interest=None)
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "illiquid_contract"


def test_illiquid_contract_blocks_untraded_contract() -> None:
    # Floor is 1 ("has it traded at all"), not a daily-volume gate:
    # OptionLegDetails.volume carries a last-trade-size proxy because
    # alpaca-py's OptionsSnapshot drops the dailyBar block. Open interest
    # is the real liquidity judgment and stays compliant here.
    opt = _option(volume=0)
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "illiquid_contract"


def test_volume_floor_is_disableable() -> None:
    """``options_min_volume=0`` must switch the gate off entirely, including
    for a None volume — the reason both call sites guard on ``> 0``."""
    caps = replace(ENABLED, options_min_volume=0)
    d = evaluate(_entry(option=_option(volume=None)), _ctx(), caps)
    assert d.approved


def test_illiquid_contract_blocks_wide_spread() -> None:
    opt = _option(bid=2.00, ask=3.00)  # (1.00)/2.50*100 = 40% — OI/volume stay compliant
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "illiquid_contract"


# ─────────────────────────────────────────────────────────────────────
# iv_unavailable
# ─────────────────────────────────────────────────────────────────────


def test_iv_unavailable_blocks_when_iv_is_null() -> None:
    opt = _option(implied_volatility=None)
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "iv_unavailable"


# ─────────────────────────────────────────────────────────────────────
# earnings_blackout
# ─────────────────────────────────────────────────────────────────────


def test_earnings_blackout_blocks_inside_the_window() -> None:
    opt = _option(days_to_earnings=1)  # default window is +-2 days
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "earnings_blackout"


# ─────────────────────────────────────────────────────────────────────
# max_premium_pct — trim then reject-below-1-contract
# ─────────────────────────────────────────────────────────────────────


def test_options_trim_names_the_rule_that_shrank_it() -> None:
    """`trimmed:10->4` is anonymous. The Refusal Ledger needs the rule
    name to report "premium cap shrank N trades" alongside its blocks."""
    d = evaluate(_entry(qty=10, last_price=2.50), _ctx(), ENABLED)
    assert d.adjusted_qty == 4
    assert d.trim_rules == ("max_premium_pct_trim",)
    assert d.approved and d.veto_rule is None


def test_options_no_trim_rules_when_size_was_left_alone() -> None:
    d = evaluate(_entry(qty=1, last_price=2.50), _ctx(), ENABLED)
    assert d.approved
    assert d.trim_rules == ()


def test_max_premium_pct_rejects_when_trim_rounds_to_zero_contracts() -> None:
    # A single contract alone ($250) is already 25% of a $1,000 account —
    # far over the 1% cap — so trimming can't produce even 1 contract.
    d = evaluate(_entry(qty=1, last_price=2.50), _ctx(account_equity=1_000.0), ENABLED)
    assert not d.approved
    assert d.veto_rule == "max_premium_pct"


# ─────────────────────────────────────────────────────────────────────
# max_total_premium_pct — portfolio aggregate
# ─────────────────────────────────────────────────────────────────────


def test_max_total_premium_pct_blocks_aggregate_over_cap() -> None:
    # Already holding $4,900 of option premium (4.9% of $100K equity).
    # Adding $250 more (qty=1 @ $2.50) -> $5,150 = 5.15%, over the 5% cap.
    held = (
        PortfolioPosition(
            "MSFT260201C00400000", 1, 49.0, 4_900.0, is_option=True, multiplier=100
        ),
    )
    d = evaluate(_entry(qty=1, last_price=2.50), _ctx(open_positions=held), ENABLED)
    assert not d.approved
    assert d.veto_rule == "max_total_premium_pct"


# ─────────────────────────────────────────────────────────────────────
# Pipeline ordering — first veto wins through evaluate_option()
# ─────────────────────────────────────────────────────────────────────


def test_expiry_day_entry_fires_before_illiquid_contract() -> None:
    """Both conditions are violated at once; expiry_day_entry (step 4)
    must win over illiquid_contract (step 7)."""
    opt = _option(expiry=_NOW.date(), open_interest=1)  # also violates illiquid_contract
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert d.veto_rule == "expiry_day_entry"


# ─────────────────────────────────────────────────────────────────────
# Anti-misrouting — the dispatch must be structural, not coincidental
# ─────────────────────────────────────────────────────────────────────


def test_occ_symbol_is_not_a_derivative_and_is_a_us_symbol() -> None:
    assert is_derivative(_OCC) is False
    assert market_of(_OCC) == "US"


# ─────────────────────────────────────────────────────────────────────
# Default-off regression
# ─────────────────────────────────────────────────────────────────────


def test_options_disabled_by_default_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_OPTIONS", raising=False)
    caps = RiskCaps.from_env()
    assert caps.options_disabled is True

    d = evaluate(_entry(), _ctx(), caps)
    assert not d.approved
    assert d.veto_rule == "options_disabled"


# ─────────────────────────────────────────────────────────────────────
# Ratchet knobs from_env — PLAN_EXIT_AGENT.md §3. Opposite polarity from
# ALLOW_OPTIONS/ALLOW_SHORTS above: unset must leave the ratchet ON.
# ─────────────────────────────────────────────────────────────────────


def test_ratchet_defaults_to_enabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPTIONS_RATCHET_ENABLED", raising=False)
    caps = RiskCaps.from_env()
    assert caps.options_ratchet_enabled is True


def test_ratchet_can_be_reverted_off_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIONS_RATCHET_ENABLED", "0")
    caps = RiskCaps.from_env()
    assert caps.options_ratchet_enabled is False


def test_ratchet_thresholds_are_env_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIONS_TRAIL_ARM_PCT", "40")
    monkeypatch.setenv("OPTIONS_TRAIL_GIVEBACK_PCT", "25")
    monkeypatch.setenv("OPTIONS_HARD_TAKE_PROFIT_PCT", "175")
    caps = RiskCaps.from_env()
    assert caps.options_trail_arm_pct == 40.0
    assert caps.options_trail_giveback_pct == 25.0
    assert caps.options_hard_take_profit_pct == 175.0


def test_malformed_ratchet_threshold_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same fail-to-default contract as every other `_env_float` cap — a
    typo must not silently produce a nonsense threshold."""
    monkeypatch.setenv("OPTIONS_TRAIL_ARM_PCT", "not-a-number")
    caps = RiskCaps.from_env()
    assert caps.options_trail_arm_pct == 35.0
