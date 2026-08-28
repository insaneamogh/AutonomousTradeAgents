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

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest

import engine.risk.engine as risk_engine_mod
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
        "min_specialist_avg_score",
        "pdt_block",
        "max_open_positions",
        "max_premium_pct",
        "max_total_premium_pct",
    ):
        assert name in d.checks_passed, f"{name} missing from {d.checks_passed}"


def test_happy_close_clears_every_rule() -> None:
    # A close never depends on options_disabled/level/DTE/liquidity/IV/
    # earnings — all entry-only. Everything else still applies.
    d = evaluate(_close(), _ctx(), RiskCaps())  # options_disabled=True, doesn't matter
    assert d.approved
    assert d.veto_rule is None


# ─────────────────────────────────────────────────────────────────────
# options_disabled
# ─────────────────────────────────────────────────────────────────────


def test_options_disabled_blocks_entry_by_default() -> None:
    d = evaluate(_entry(), _ctx(), RiskCaps())  # options_disabled=True (default)
    assert not d.approved
    assert d.veto_rule == "options_disabled"


def test_options_disabled_off_lets_entry_through() -> None:
    d = evaluate(_entry(), _ctx(), ENABLED)
    assert d.approved


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


def test_naked_short_forbidden_allows_buy_to_open_and_sell_to_close() -> None:
    assert evaluate(_entry(), _ctx(), ENABLED).veto_rule != "naked_short_forbidden"
    assert evaluate(_close(), _ctx(), ENABLED).veto_rule != "naked_short_forbidden"


# ─────────────────────────────────────────────────────────────────────
# options_level_insufficient
# ─────────────────────────────────────────────────────────────────────


def test_options_level_insufficient_at_level_1() -> None:
    # Alpaca level 1 = assignment-bearing structures (Phase C) — a LOWER
    # number than Phase A's long call/put floor of 2.
    d = evaluate(_entry(), _ctx(options_trading_level=1), ENABLED)
    assert not d.approved
    assert d.veto_rule == "options_level_insufficient"


def test_options_level_insufficient_at_level_2_passes() -> None:
    d = evaluate(_entry(), _ctx(options_trading_level=2), ENABLED)
    assert d.veto_rule != "options_level_insufficient"


def test_options_level_insufficient_at_level_3_passes() -> None:
    d = evaluate(_entry(), _ctx(options_trading_level=3), ENABLED)
    assert d.veto_rule != "options_level_insufficient"


def test_options_level_insufficient_when_level_unknown() -> None:
    d = evaluate(_entry(), _ctx(options_trading_level=None), ENABLED)
    assert not d.approved
    assert d.veto_rule == "options_level_insufficient"


def test_options_level_insufficient_never_blocks_a_close() -> None:
    d = evaluate(_close(), _ctx(options_trading_level=None), ENABLED)
    assert d.veto_rule != "options_level_insufficient"


# ─────────────────────────────────────────────────────────────────────
# expiry_day_entry
# ─────────────────────────────────────────────────────────────────────


def test_expiry_day_entry_blocks_a_new_position_expiring_today() -> None:
    opt = _option(expiry=_NOW.date())
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "expiry_day_entry"


def test_expiry_day_entry_never_blocks_a_same_day_close() -> None:
    opt = _option(expiry=_NOW.date(), action="sell_to_close")
    d = evaluate(_close(option=opt), _ctx(), ENABLED)
    assert d.veto_rule != "expiry_day_entry"


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


def test_dte_boundaries_never_block_a_close() -> None:
    near = _option(expiry=date(2026, 1, 12), action="sell_to_close")  # 2 dte
    far = _option(expiry=date(2026, 6, 1), action="sell_to_close")  # far past 60 dte
    assert evaluate(_close(option=near), _ctx(), ENABLED).veto_rule not in (
        "min_dte", "max_dte",
    )
    assert evaluate(_close(option=far), _ctx(), ENABLED).veto_rule not in (
        "min_dte", "max_dte",
    )


# ─────────────────────────────────────────────────────────────────────
# illiquid_contract — three independent sub-conditions
# ─────────────────────────────────────────────────────────────────────


def test_illiquid_contract_blocks_low_open_interest() -> None:
    opt = _option(open_interest=50)  # floor is 100
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "illiquid_contract"


def test_illiquid_contract_blocks_missing_open_interest() -> None:
    opt = _option(open_interest=None)
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "illiquid_contract"


def test_illiquid_contract_blocks_low_volume() -> None:
    opt = _option(volume=5)  # floor is 10; OI stays compliant
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "illiquid_contract"


def test_illiquid_contract_blocks_wide_spread() -> None:
    opt = _option(bid=2.00, ask=3.00)  # (1.00)/2.50*100 = 40% — OI/volume stay compliant
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "illiquid_contract"


def test_illiquid_contract_never_blocks_a_close() -> None:
    opt = _option(open_interest=0, volume=0, bid=None, ask=None, action="sell_to_close")
    d = evaluate(_close(option=opt), _ctx(), ENABLED)
    assert d.veto_rule != "illiquid_contract"


# ─────────────────────────────────────────────────────────────────────
# iv_unavailable
# ─────────────────────────────────────────────────────────────────────


def test_iv_unavailable_blocks_when_iv_is_null() -> None:
    opt = _option(implied_volatility=None)
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "iv_unavailable"


def test_iv_unavailable_passes_when_iv_is_present() -> None:
    opt = _option(implied_volatility=0.31)
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert d.veto_rule != "iv_unavailable"


def test_iv_unavailable_never_blocks_a_close() -> None:
    opt = _option(implied_volatility=None, action="sell_to_close")
    d = evaluate(_close(option=opt), _ctx(), ENABLED)
    assert d.veto_rule != "iv_unavailable"


# ─────────────────────────────────────────────────────────────────────
# earnings_blackout
# ─────────────────────────────────────────────────────────────────────


def test_earnings_blackout_blocks_inside_the_window() -> None:
    opt = _option(days_to_earnings=1)  # default window is +-2 days
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert not d.approved
    assert d.veto_rule == "earnings_blackout"


def test_earnings_blackout_passes_outside_the_window() -> None:
    opt = _option(days_to_earnings=10)
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert d.veto_rule != "earnings_blackout"


def test_earnings_blackout_self_gates_when_unknown() -> None:
    """Missing earnings-calendar data must not halt trading — the same
    "missing data doesn't halt" principle used elsewhere in this rule set."""
    opt = _option(days_to_earnings=None)
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert d.veto_rule != "earnings_blackout"
    assert d.approved


# ─────────────────────────────────────────────────────────────────────
# max_premium_pct — trim then reject-below-1-contract
# ─────────────────────────────────────────────────────────────────────


def test_max_premium_pct_trims_when_over_cap() -> None:
    # qty=10 @ $2.50 x100 = $2,500/contract * 10 = $25,000 premium = 25%
    # of $100K equity. Cap is 1% -> trims to 4 contracts ($1,000 = 1%).
    d = evaluate(_entry(qty=10, last_price=2.50), _ctx(), ENABLED)
    assert d.approved
    assert d.adjusted_qty == 4
    assert any("trimmed" in f for f in d.informational_flags)


def test_max_premium_pct_rejects_when_trim_rounds_to_zero_contracts() -> None:
    # A single contract alone ($250) is already 25% of a $1,000 account —
    # far over the 1% cap — so trimming can't produce even 1 contract.
    d = evaluate(_entry(qty=1, last_price=2.50), _ctx(account_equity=1_000.0), ENABLED)
    assert not d.approved
    assert d.veto_rule == "max_premium_pct"


def test_max_premium_pct_never_blocks_a_close() -> None:
    d = evaluate(_close(qty=10, last_price=2.50), _ctx(account_equity=1_000.0), ENABLED)
    assert d.veto_rule != "max_premium_pct"


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


def test_max_total_premium_pct_ignores_equity_positions() -> None:
    """A held EQUITY position's market_value must not count toward the
    options-only aggregate — is_option=False positions are excluded."""
    held = (PortfolioPosition("NVDA", 100, 490.0, 49_000.0, is_option=False),)
    d = evaluate(_entry(qty=1, last_price=2.50), _ctx(open_positions=held), ENABLED)
    assert d.veto_rule != "max_total_premium_pct"


def test_max_total_premium_pct_never_blocks_a_close() -> None:
    held = (
        PortfolioPosition(
            "MSFT260201C00400000", 100, 49.0, 490_000.0, is_option=True, multiplier=100
        ),
    )
    d = evaluate(_close(qty=1, last_price=2.50), _ctx(open_positions=held), ENABLED)
    assert d.veto_rule != "max_total_premium_pct"


# ─────────────────────────────────────────────────────────────────────
# Pipeline ordering — first veto wins through evaluate_option()
# ─────────────────────────────────────────────────────────────────────


def test_expiry_day_entry_fires_before_illiquid_contract() -> None:
    """Both conditions are violated at once; expiry_day_entry (step 4)
    must win over illiquid_contract (step 7)."""
    opt = _option(expiry=_NOW.date(), open_interest=1)  # also violates illiquid_contract
    d = evaluate(_entry(option=opt), _ctx(), ENABLED)
    assert d.veto_rule == "expiry_day_entry"


def test_options_level_insufficient_fires_before_illiquid_contract() -> None:
    """Both conditions are violated at once; options_level_insufficient
    (earlier in the sequence) must win over illiquid_contract (later)."""
    opt = _option(open_interest=1)  # would also veto illiquid_contract
    d = evaluate(_entry(option=opt), _ctx(options_trading_level=1), ENABLED)
    assert d.veto_rule == "options_level_insufficient"


# ─────────────────────────────────────────────────────────────────────
# Anti-misrouting — the dispatch must be structural, not coincidental
# ─────────────────────────────────────────────────────────────────────


def test_occ_symbol_is_not_a_derivative_and_is_a_us_symbol() -> None:
    assert is_derivative(_OCC) is False
    assert market_of(_OCC) == "US"


def test_options_proposal_never_reaches_equity_only_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural proof, not a behavioral coincidence: patch every
    equity-only rule this dispatch must protect against with a spy that
    records whether it was ever CALLED, then confirm zero calls for an
    options proposal run through the TOP-LEVEL evaluate(). Absence from
    ``checks_passed`` alone wouldn't distinguish "never ran" from "ran and
    vetoed" for rules that never add themselves to that list even when
    they DO run (derivative_notional_cap, for one) — call-count is the
    only assertion that can't be fooled that way.
    """
    calls: dict[str, int] = {}

    def _spy(name: str) -> Callable[..., None]:
        def _fn(*_args: object, **_kwargs: object) -> None:
            calls[name] = calls.get(name, 0) + 1
            return None
        return _fn

    equity_only_rule_names = (
        "position_size_cap",
        "sector_concentration",
        "single_name_concentration",
        "correlation_cap",
        "derivative_notional_cap",
        "lot_size_block",
    )
    for name in equity_only_rule_names:
        monkeypatch.setattr(risk_engine_mod, name, _spy(name))

    # A deliberately absurd qty/notional — big enough that ANY of the
    # patched rules would trip (or at least run) if the dispatch failed
    # and this proposal fell through to the equity chain instead.
    proposal = _entry(qty=1_000_000, last_price=2.50)
    decision = evaluate(proposal, _ctx(), ENABLED)

    assert calls == {}, f"equity-only rules were called: {calls}"
    for name in equity_only_rule_names:
        assert name not in decision.checks_passed

    # The India-lot-size-violating qty specifically must not veto through
    # derivative_notional_cap — proving the India rule genuinely never
    # ran on this US options symbol, not merely that it happened to pass.
    assert decision.veto_rule != "derivative_notional_cap"
    assert decision.veto_rule != "lot_size_block"


def test_options_never_reaches_short_requires_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-boundary regression, mirroring the short-position precedent:
    an options RiskProposal (stop_price=None, since to_risk_proposal never
    sets it) must not trip short_requires_stop — proven by patching the
    rule with a spy, not by asserting the proposal happens to pass today.
    """
    calls: list[int] = []

    def _spy(*_args: object, **_kwargs: object) -> None:
        calls.append(1)
        return None

    monkeypatch.setattr(risk_engine_mod, "short_requires_stop", _spy)

    proposal = _entry()
    assert proposal.stop_price is None
    decision = evaluate(proposal, _ctx(), ENABLED)

    assert calls == []
    assert decision.veto_rule != "short_requires_stop"


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
