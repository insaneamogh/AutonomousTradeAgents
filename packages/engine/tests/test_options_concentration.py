"""Per-underlying and per-direction concentration caps on the options book.

These exist because of a measured failure, not a hypothetical: on
2026-09-04 the live book held four separate NVDA calls across three
expiries ($4,840 = 5.0% of equity) and two GILD calls, and every aggregate
cap passed. The equity path's ``single_name_concentration`` could not have
caught it either — it compares ``p.symbol == proposal.symbol``, and on the
options path ``symbol`` is the OCC string, so four strikes read as four
different names.

Fixtures mirror ``test_options_risk.py``: every proposal is otherwise
compliant so a veto can only be attributed to the rule under test, and
everything runs through the top-level ``evaluate()`` so the ``is_option``
dispatch stays covered.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from engine.options.contracts import to_risk_proposal
from engine.options.rules import occ_contract_type, occ_root
from engine.risk import (
    OptionLegDetails,
    PortfolioPosition,
    RiskCaps,
    RiskContext,
    RiskProposal,
    Side,
    evaluate,
)

_NOW = datetime(2026, 1, 10, 15, 0, tzinfo=UTC)
_EQUITY = 100_000.0


def _caps(**overrides: object) -> RiskCaps:
    base: dict[str, object] = dict(
        options_max_single_underlying_pct=2.0,
        options_max_direction_pct=65.0,
        min_council_confidence=0.30,   # not under test here
        options_max_total_premium_pct=50.0,  # keep the aggregate cap out of the way
        options_max_premium_pct=50.0,
        options_disabled=False,
    )
    base.update(overrides)
    return RiskCaps(**base)  # type: ignore[arg-type]


def _ctx(positions: tuple[PortfolioPosition, ...] = (), **overrides: object) -> RiskContext:
    base: dict[str, object] = dict(
        account_equity=_EQUITY,
        cash=_EQUITY,
        buying_power=_EQUITY,
        options_trading_level=3,
        now_utc=_NOW,
        open_positions=positions,
    )
    base.update(overrides)
    return RiskContext(**base)  # type: ignore[arg-type]


def _held(occ: str, premium: float) -> PortfolioPosition:
    return PortfolioPosition(
        symbol=occ, qty=1, avg_entry_price=premium / 100.0,
        market_value=premium, is_option=True, multiplier=100,
    )


def _entry(occ: str, underlying: str, ctype: str, premium: float) -> RiskProposal:
    opt = OptionLegDetails(
        underlying_symbol=underlying, occ_symbol=occ, contract_type=ctype,
        strike=250.0, expiry=date(2026, 2, 1), multiplier=100,
        action="buy_to_open", open_interest=500, volume=100,
        bid=(premium / 100.0) * 0.98, ask=(premium / 100.0) * 1.02,
        implied_volatility=0.28, days_to_earnings=None,
    )
    return to_risk_proposal(
        symbol=occ, side=Side.BUY, qty=1,
        estimated_notional=premium, last_price=premium / 100.0,
        confidence=0.70, option=opt,
    )


# ── the OCC parse, which the whole grouping rests on ──────────────────


@pytest.mark.parametrize(
    "symbol,root,kind",
    [
        ("NVDA261002C00230000", "NVDA", "call"),
        ("TQQQ260918P00071000", "TQQQ", "put"),
        ("F261016C00015000", "F", "call"),        # 1-char root
        ("AAPL", None, None),                      # plain equity ticker
        ("", None, None),
        ("NVDA26XX02C00230000", None, None),       # non-numeric date
        ("NVDA261002X00230000", None, None),       # not C or P
    ],
)
def test_occ_parse(symbol: str, root: str | None, kind: str | None) -> None:
    assert occ_root(symbol) == root
    assert occ_contract_type(symbol) == kind


# ── single-underlying cap ─────────────────────────────────────────────


def test_second_strike_on_the_same_underlying_is_refused() -> None:
    """The exact 2026-09-04 failure: a second NVDA call at a DIFFERENT
    strike and expiry. Reverting the rule lets this through."""
    ctx = _ctx((_held("NVDA261002C00230000", 1_070.0),))
    d = evaluate(_entry("NVDA261009C00245000", "NVDA", "call", 1_440.0), ctx, _caps())
    assert not d.approved
    assert d.veto_rule == "options_single_underlying_cap"
    assert "NVDA" in d.reason


def test_grouping_is_by_underlying_not_by_occ_symbol() -> None:
    """Three different NVDA OCC strings must aggregate to ONE name. This is
    the assertion that ``single_name_concentration`` could never make."""
    held = (
        _held("NVDA261002C00230000", 700.0),
        _held("NVDA261009C00245000", 700.0),
        _held("NVDA261016C00235000", 500.0),
    )
    # 1,900 held + 300 new = 2,200 = 2.2% > 2.0% cap. Only aggregation gets there;
    # no single held position is close on its own.
    d = evaluate(_entry("NVDA261023C00250000", "NVDA", "call", 300.0), _ctx(held), _caps())
    assert not d.approved
    assert d.veto_rule == "options_single_underlying_cap"


def test_a_different_underlying_is_unaffected() -> None:
    ctx = _ctx((_held("NVDA261002C00230000", 1_900.0),))
    d = evaluate(_entry("GILD261016C00150000", "GILD", "call", 1_160.0), ctx, _caps())
    assert d.approved, d.reason


def test_first_position_on_a_name_within_cap_is_allowed() -> None:
    d = evaluate(_entry("NVDA261002C00230000", "NVDA", "call", 1_070.0), _ctx(), _caps())
    assert d.approved, d.reason


def test_equity_positions_never_count_toward_an_option_name() -> None:
    """A held NVDA *share* position is not option premium and must not
    consume the option book's per-name budget."""
    shares = PortfolioPosition(
        symbol="NVDA", qty=100, avg_entry_price=180.0,
        market_value=18_000.0, is_option=False, multiplier=1,
    )
    d = evaluate(_entry("NVDA261002C00230000", "NVDA", "call", 1_070.0), _ctx((shares,)), _caps())
    assert d.approved, d.reason


# ── direction cap ─────────────────────────────────────────────────────


def _three_calls() -> tuple[PortfolioPosition, ...]:
    return (
        _held("NVDA261002C00230000", 1_000.0),
        _held("GILD261016C00150000", 1_000.0),
        _held("XLF261002C00058000", 855.0),
    )


def test_fourth_call_on_an_all_call_book_is_refused() -> None:
    d = evaluate(_entry("ARKK261016C00086000", "ARKK", "call", 900.0),
                 _ctx(_three_calls()), _caps())
    assert not d.approved
    assert d.veto_rule == "options_direction_cap"


def test_a_put_is_allowed_on_that_same_all_call_book() -> None:
    """The cap must block only the side that breaches — otherwise the book
    can never rebalance out of the corner it is in."""
    d = evaluate(_entry("TQQQ260918P00071000", "TQQQ", "put", 1_265.0),
                 _ctx(_three_calls()), _caps())
    assert d.approved, d.reason


def test_direction_cap_does_not_bind_on_a_book_too_small_to_have_a_ratio() -> None:
    """Regression: applied from the first trade, a 65% ratio refuses 100%
    of trades — the first position is always 100% of its own side. The
    first version of this rule admitted 0 of 11 positions in replay."""
    for held in ((), _three_calls()[:1], _three_calls()[:2]):
        d = evaluate(_entry("ARKK261016C00086000", "ARKK", "call", 900.0),
                     _ctx(held), _caps())
        assert d.approved, f"book of {len(held)} must not bind: {d.reason}"


def test_calls_are_allowed_again_once_puts_restore_the_ratio() -> None:
    book = (
        *_three_calls(),
        _held("TQQQ260918P00071000", 1_265.0),
        _held("VXX261016P00017000", 1_248.0),
    )
    d = evaluate(_entry("ARKK261016C00086000", "ARKK", "call", 900.0), _ctx(book), _caps())
    assert d.approved, d.reason
