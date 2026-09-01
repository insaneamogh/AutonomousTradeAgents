"""Ghost evaluator unit tests — pure deterministic pieces.

DB-touching paths are covered by the staging smoke (daily_cron --force
twice); here we pin the math: entry derivation, P&L direction, trading
day offsets, synthetic provider determinism.
"""

from __future__ import annotations

from datetime import date

import pytest

from engine.prices import SyntheticPriceProvider
from trading_agents.jobs.ghost_eval import (
    _entry_price,
    _ghost_pnl,
    _is_option,
    _mark_symbol,
    _multiplier,
    _skip_reason,
    trading_day_offset,
)


def test_entry_price_prefers_limit() -> None:
    assert _entry_price({"limitPrice": 101.5, "qty": 10, "estimatedNotional": 990}) == (
        101.5,
        "proposal_limit",
    )


def test_entry_price_falls_back_to_notional() -> None:
    price, source = _entry_price({"qty": 20, "estimatedNotional": 4810.0})
    assert source == "proposal_notional"
    assert price == pytest.approx(240.5)


def test_entry_price_none_when_unusable() -> None:
    assert _entry_price({}) is None
    assert _entry_price({"qty": 0, "estimatedNotional": 100}) is None


def test_ghost_pnl_directions() -> None:
    # BUY: price up = gain; SELL: price up = loss.
    assert _ghost_pnl("BUY", 10, 100.0, 105.0) == 50.0
    assert _ghost_pnl("BUY", 10, 100.0, 95.0) == -50.0
    assert _ghost_pnl("SELL", 10, 100.0, 105.0) == -50.0
    assert _ghost_pnl("SELL", 10, 100.0, 95.0) == 50.0


def test_trading_day_offset_skips_weekends() -> None:
    friday = date(2026, 6, 5)
    monday = date(2026, 6, 8)
    tuesday = date(2026, 6, 9)
    assert trading_day_offset(friday, friday) == 0
    assert trading_day_offset(friday, monday) == 1
    assert trading_day_offset(friday, tuesday) == 2


@pytest.mark.asyncio
async def test_synthetic_provider_is_deterministic_and_anchored() -> None:
    p1 = SyntheticPriceProvider(anchor_price=200.0, anchor_day=date(2026, 6, 1))
    p2 = SyntheticPriceProvider(anchor_price=200.0, anchor_day=date(2026, 6, 1))
    a = await p1.daily_closes("NVDA", date(2026, 6, 1), date(2026, 6, 10))
    b = await p2.daily_closes("NVDA", date(2026, 6, 1), date(2026, 6, 10))
    assert a == b
    assert a[0].close == 200.0  # anchor day pins the price
    assert all(c.day.weekday() < 5 for c in a)  # no weekend bars
    # Different symbol → different walk.
    c = await p1.daily_closes("TSLA", date(2026, 6, 1), date(2026, 6, 10))
    assert [x.close for x in c][1:] != [x.close for x in a][1:]


@pytest.mark.asyncio
async def test_synthetic_provider_empty_when_inverted_window() -> None:
    p = SyntheticPriceProvider()
    assert await p.daily_closes("NVDA", date(2026, 6, 10), date(2026, 6, 1)) == []


# ── Options ghosts ────────────────────────────────────────────────────
# Every test below fails if the options branch is removed — the whole
# point is that an options refusal is marked on its OWN contract, in the
# right units. Verified by reverting each change in turn.


def _option_proposal(**over: object) -> dict:
    base = {
        "isOption": True,
        "occSymbol": "nvda260918c00250000",
        "multiplier": 100,
        "qty": 4,
        "limitPrice": 2.17,
        "estimatedNotional": 868.0,
    }
    base.update(over)
    return base


def test_mark_symbol_is_the_occ_contract_not_the_underlying() -> None:
    """The stock-bars endpoint returns [] (not an error) for an OCC
    symbol, so getting this wrong is silent."""

    class _Row:
        symbol = "NVDA"

    assert _mark_symbol(_Row(), _option_proposal()) == "NVDA260918C00250000"
    assert _mark_symbol(_Row(), {"qty": 10}) == "NVDA"


def test_mark_symbol_skips_an_option_row_with_no_occ() -> None:
    """None means 'skip', never 'fall back to the underlying' — marking
    the stock would put a plausible-looking wrong number in the ledger."""

    class _Row:
        symbol = "NVDA"

    assert _mark_symbol(_Row(), _option_proposal(occSymbol=None)) is None


def test_option_entry_from_notional_divides_out_the_multiplier() -> None:
    """estimatedNotional is premium * qty * 100; option bars quote the
    per-share premium. Without the divide, $2.17 reads as $217."""
    price, source = _entry_price(_option_proposal(limitPrice=None))
    assert source == "proposal_notional"
    assert price == pytest.approx(2.17)


def test_equity_entry_from_notional_is_unchanged_by_the_option_path() -> None:
    price, _ = _entry_price({"qty": 20, "estimatedNotional": 4810.0})
    assert price == pytest.approx(240.5)


def test_ghost_pnl_scales_options_by_the_contract_multiplier() -> None:
    """A $1.23 premium move on 4 contracts is $492, not $4.92."""
    assert _ghost_pnl("BUY", 4, 2.17, 3.40, 100) == 492.0
    # Equity default stays 1x.
    assert _ghost_pnl("BUY", 4, 2.17, 3.40) == 4.92


def test_multiplier_defaults_are_instrument_correct() -> None:
    assert _multiplier({"qty": 1}) == 1  # equity
    assert _multiplier({"isOption": True}) == 100  # option, key absent
    assert _multiplier(_option_proposal(multiplier=0)) == 100  # nonsense → 100
    assert _multiplier(_option_proposal(multiplier="bad")) == 100


def test_is_option_accepts_both_key_styles() -> None:
    assert _is_option({"isOption": True})
    assert _is_option({"is_option": True})
    assert not _is_option({})


def test_entry_price_accepts_snake_case_notional() -> None:
    """A vetoed proposal is persisted as the Drafter's raw dict
    (`estimated_notional`), not the camelCase DTO — the write-side gap
    IMPL_REFUSAL_LEDGER.md §0 traces the live "$0 blocked" ledger to.
    `_entry_price` must find it under either key."""
    price, source = _entry_price({"qty": 16, "estimated_notional": 4922.08})
    assert source == "proposal_notional"
    assert price == pytest.approx(4922.08 / 16)


def test_entry_price_accepts_snake_case_limit() -> None:
    price, source = _entry_price({"limit_price": 12.5, "qty": 10, "estimated_notional": 125.0})
    assert source == "proposal_limit"
    assert price == 12.5


# ── test_ghost_eval_counters_name_the_skip_reason ──────────────────────
# `_skip_reason` is the pure prefilter `evaluate_ghosts` uses to name WHICH
# check failed. Pinned here (no DB needed) rather than on the DB-touching
# `evaluate_ghosts` itself — same split as the rest of this file.


def _ok_args(**over: object) -> dict:
    base: dict = {
        "reason": "vetoed",
        "entry": (100.0, "proposal_limit"),
        "mark_symbol": "NVDA",
        "side": "BUY",
        "qty": 10,
    }
    base.update(over)
    return base


def test_skip_reason_none_when_everything_is_usable() -> None:
    assert _skip_reason(**_ok_args()) is None


def test_skip_reason_names_reason_is_none() -> None:
    assert _skip_reason(**_ok_args(reason=None)) == "reason_is_none"


def test_skip_reason_names_entry_is_none() -> None:
    assert _skip_reason(**_ok_args(entry=None)) == "entry_is_none"


def test_skip_reason_names_mark_symbol_is_none() -> None:
    assert _skip_reason(**_ok_args(mark_symbol=None)) == "mark_symbol_is_none"


def test_skip_reason_names_bad_side() -> None:
    assert _skip_reason(**_ok_args(side="SHORT")) == "bad_side"


def test_skip_reason_names_falsy_qty() -> None:
    assert _skip_reason(**_ok_args(qty=0)) == "falsy_qty"


def test_skip_reason_checks_in_priority_order() -> None:
    """When several checks would fail at once, the first one in the
    doc's own listed order wins -- pins the order so the counters stay
    stable across refactors."""
    assert _skip_reason(**_ok_args(reason=None, entry=None)) == "reason_is_none"
