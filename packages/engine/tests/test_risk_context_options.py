"""Options plumbing through the two RiskContext providers.

Council-time risk checks read RiskContext from these providers, a
separate path from the executor's own live broker call — without this
plumbing, RiskContext.options_trading_level is always None and
options_level_insufficient vetoes every options proposal unconditionally,
regardless of the real account's approval tier.
"""

from __future__ import annotations

from engine.risk.context import MockRiskContextProvider
from engine.risk.postgres_context import _parse_positions


async def test_mock_provider_defaults_options_trading_level_to_3() -> None:
    """Defaults to 3, not None, so mock-mode/CI can exercise the options
    path without extra wiring — see docs/OPTIONS_PLAN.md's live-account
    check for why 3 is the realistic default."""
    ctx = await MockRiskContextProvider().fetch()
    assert ctx.options_trading_level == 3


async def test_mock_provider_options_trading_level_is_overridable() -> None:
    ctx = await MockRiskContextProvider(options_trading_level=1).fetch()
    assert ctx.options_trading_level == 1


def test_parse_positions_reads_is_option_and_multiplier() -> None:
    rows = [
        {
            "symbol": "AAPL260828C00250000",
            "qty": 2,
            "avg_entry_price": 3.20,
            "market_value": 640.0,
            "sector": None,
            "is_option": True,
            "multiplier": 100,
        },
        {
            "symbol": "MSFT",
            "qty": 10,
            "avg_entry_price": 300.0,
            "market_value": 3000.0,
            "sector": "tech",
        },
    ]
    positions = _parse_positions(rows)

    assert len(positions) == 2
    option_row = next(p for p in positions if p.symbol == "AAPL260828C00250000")
    assert option_row.is_option is True
    assert option_row.multiplier == 100

    equity_row = next(p for p in positions if p.symbol == "MSFT")
    assert equity_row.is_option is False
    assert equity_row.multiplier == 1
