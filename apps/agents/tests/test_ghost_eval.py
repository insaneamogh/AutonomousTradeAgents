"""Ghost evaluator unit tests — pure deterministic pieces.

DB-touching paths are covered by the staging smoke (daily_cron --force
twice); here we pin the math: entry derivation, P&L direction, trading
day offsets, synthetic provider determinism.
"""

from __future__ import annotations

from datetime import date

import pytest
from engine.prices import SyntheticPriceProvider
from scripts.ghost_eval import _entry_price, _ghost_pnl, _trading_day_offset


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
    assert _trading_day_offset(friday, friday) == 0
    assert _trading_day_offset(friday, monday) == 1
    assert _trading_day_offset(friday, tuesday) == 2


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
