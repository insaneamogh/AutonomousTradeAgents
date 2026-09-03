"""The production poller must report the options trading level.

`UserBrokerPoller` — not `engine.reconciler.poller.AlpacaPoller` — is what
the running API uses. It omitted `options_trading_level`, so every snapshot
wrote None, and `options_level_insufficient` vetoed every option entry on an
account approved for level 3. Nothing raised; the snapshot is the only
source `postgres_context` has for the level.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.services.orders import reconciler_fleet as rf


@dataclass
class _FakeBroker:
    level: int | None = 3

    async def get_account_equity(self) -> float:
        return 100_000.0

    async def get_buying_power(self) -> float:
        return 400_000.0

    async def get_options_trading_level(self) -> int | None:
        return self.level

    async def list_positions(self):
        return [
            SimpleNamespace(
                symbol="NVDA260918C00250000", qty=4, avg_entry_price=2.17,
                market_value=868.0, is_option=True, multiplier=100,
                unrealized_pl=20.0,
            ),
            SimpleNamespace(
                symbol="AAPL", qty=10, avg_entry_price=200.0,
                market_value=2000.0, is_option=False, multiplier=1,
                unrealized_pl=-5.0,
            ),
        ]


def _patch_broker(monkeypatch: pytest.MonkeyPatch, fake: _FakeBroker) -> None:
    @contextlib.asynccontextmanager
    async def _fake_cm(user_id: str, **kwargs: object):
        yield fake, SimpleNamespace(is_paper=True, id="conn-1")

    monkeypatch.setattr(rf, "with_broker_client", _fake_cm)


async def test_poller_reports_the_options_trading_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_broker(monkeypatch, _FakeBroker(level=3))
    state = await rf.UserBrokerPoller(user_id="u1").get_account_state()
    assert state.options_trading_level == 3


async def test_poller_passes_through_a_missing_level_rather_than_inventing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None must stay None — an account with no options approval must keep
    tripping `options_level_insufficient`, not be defaulted to a level."""
    _patch_broker(monkeypatch, _FakeBroker(level=None))
    state = await rf.UserBrokerPoller(user_id="u1").get_account_state()
    assert state.options_trading_level is None


async def test_poller_marks_option_positions_as_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without is_option/multiplier an option position enters the risk
    context looking like stock, and `max_total_premium_pct` under-counts."""
    _patch_broker(monkeypatch, _FakeBroker())
    state = await rf.UserBrokerPoller(user_id="u1").get_account_state()
    by_symbol = {p.symbol: p for p in state.open_positions}
    opt = by_symbol["NVDA260918C00250000"]
    assert opt.is_option is True
    assert opt.multiplier == 100
    eq = by_symbol["AAPL"]
    assert eq.is_option is False
    assert eq.multiplier == 1
