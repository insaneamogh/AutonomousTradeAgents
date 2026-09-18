"""`RiskContext.now_utc` must actually reach the time-dependent rules.

The field existed from the start and **nothing outside tests ever set it**,
so `expiry_day_entry`, `min_dte`, `max_dte` and `mis_square_off_block` all
fell through their `context.now_utc or datetime.now(UTC)` guard to the
process wall clock.

Two clocks, then: the market-open gate resolves through
`alpaca clock` -> REST -> local calendar, while the DTE rules used whatever
the container believed. Usually the same answer, which is why it never
showed up as a production incident — but it made time-dependent behaviour
untestable, and that is how 14 tests came to pin an expiry date
(2026-09-18) that eventually arrived and started tripping
`expiry_day_entry` on every happy-path test in the options suite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from engine.options.rules import expiry_day_entry, min_dte
from engine.risk import (
    MockRiskContextProvider,
    OptionLegDetails,
    RiskCaps,
    RiskContext,
    RiskProposal,
    Side,
)

_EXPIRY = date(2026, 9, 18)


def _proposal() -> RiskProposal:
    opt = OptionLegDetails(
        underlying_symbol="NVDA", occ_symbol="NVDA260918C00225000",
        contract_type="call", strike=225.0, expiry=_EXPIRY,
        multiplier=100, action="buy_to_open", bid=2.45, ask=2.55,
        implied_volatility=0.28,
    )
    return RiskProposal(
        symbol=opt.occ_symbol, side=Side.BUY, qty=1, last_price=2.50,
        estimated_notional=250.0, confidence=0.7, is_option=True, option=opt,
    )


def _ctx(now: datetime | None) -> RiskContext:
    return RiskContext(
        account_equity=100_000.0, cash=100_000.0, buying_power=100_000.0,
        options_trading_level=3, now_utc=now,
    )


async def test_the_provider_threads_its_clock_through() -> None:
    """The wiring that was missing. Without this the field is decorative."""
    pinned = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    ctx = await MockRiskContextProvider(now_utc=pinned).fetch()
    assert ctx.now_utc == pinned


async def test_an_unset_clock_still_defaults_to_none() -> None:
    """Unchanged behaviour for any caller that does not opt in — the rules
    keep their wall-clock fallback."""
    assert (await MockRiskContextProvider().fetch()).now_utc is None


def test_expiry_day_entry_honours_the_injected_clock() -> None:
    """Sixteen days before expiry it must NOT fire, whatever today is."""
    caps = RiskCaps(options_disabled=False)
    before = _ctx(datetime(2026, 9, 2, 15, 0, tzinfo=UTC))
    assert expiry_day_entry(_proposal(), before, caps) is None


def test_expiry_day_entry_fires_on_the_injected_expiry_day() -> None:
    caps = RiskCaps(options_disabled=False)
    on_the_day = _ctx(datetime(2026, 9, 18, 15, 0, tzinfo=UTC))
    d = expiry_day_entry(_proposal(), on_the_day, caps)
    assert d is not None and d.veto_rule == "expiry_day_entry"


def test_dte_rules_read_the_same_clock() -> None:
    """Not just `expiry_day_entry` — every rule that asks what day it is
    must agree, or the engine can refuse and permit the same contract in
    one pass."""
    caps = RiskCaps(options_disabled=False, options_min_dte=7)
    assert min_dte(_proposal(), _ctx(datetime(2026, 9, 2, 15, 0, tzinfo=UTC)), caps) is None
    late = min_dte(_proposal(), _ctx(datetime(2026, 9, 16, 15, 0, tzinfo=UTC)), caps)
    assert late is not None and late.veto_rule == "min_dte"


@pytest.mark.parametrize("rule", [expiry_day_entry, min_dte])
def test_no_rule_crashes_without_a_clock(rule) -> None:
    """The fallback must stay — production does not inject one yet."""
    rule(_proposal(), _ctx(None), RiskCaps(options_disabled=False))
