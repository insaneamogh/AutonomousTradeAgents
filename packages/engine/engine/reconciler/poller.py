"""BrokerPoller Protocol + implementations.

Wraps the broker so the reconciler doesn't take a hard dep on ``broker.alpaca``.
That keeps the reconciler testable offline (MockBrokerPoller) and decoupled
from broker-SDK lifecycle.

Two implementations ship:
  - ``MockBrokerPoller``      deterministic synthetic state; configurable.
  - ``AlpacaBrokerPoller``    adapter over ``broker.alpaca.AlpacaBroker``.

Phase 0/1 default = Mock. Phase 2 swaps to Alpaca after paper validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from engine.risk import PortfolioPosition

if TYPE_CHECKING:
    from broker.alpaca import AlpacaBroker


@dataclass(frozen=True)
class RawAccountState:
    """Broker-agnostic account snapshot — input to ``snapshot.write_snapshot``."""

    equity: float
    cash: float
    buying_power: float
    open_positions: tuple[PortfolioPosition, ...] = ()
    options_trading_level: int | None = None
    prior_close_equity: float | None = None
    """Equity at the PREVIOUS trading session's close, when the broker
    reports it (Alpaca's ``last_equity``). ``snapshot._daily_pnl`` prefers
    this over any locally-derived baseline — see that function for why a
    UTC-day baseline silently drops the overnight move."""
    raw: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class BrokerPoller(Protocol):
    """Anything that can produce a ``RawAccountState`` is a poller."""

    name: str

    async def get_account_state(self) -> RawAccountState: ...


@dataclass
class MockBrokerPoller:
    """In-memory poller. Defaults to a flat $100K paper account.

    Tests + offline dev mutate the fields directly to simulate scenarios:
        poller = MockBrokerPoller(equity=97_000.0)  # 3% drawdown
    """

    equity: float = 100_000.0
    cash: float = 100_000.0
    buying_power: float = 200_000.0
    positions: tuple[PortfolioPosition, ...] = ()
    options_trading_level: int | None = 3
    """Defaults to 3 (Alpaca's own "spreads + long/short singles" tier —
    see docs/OPTIONS_PLAN.md's live-account check) rather than None, so
    mock-mode/CI can exercise the options path without extra wiring.
    Override to test ``options_level_insufficient`` explicitly."""
    name: str = "mock"

    async def get_account_state(self) -> RawAccountState:
        return RawAccountState(
            equity=self.equity,
            cash=self.cash,
            buying_power=self.buying_power,
            open_positions=tuple(self.positions),
            options_trading_level=self.options_trading_level,
            raw={"source": "mock", "equity": self.equity},
        )


@dataclass
class AlpacaBrokerPoller:
    """Adapter over the existing AlpacaBroker. Phase 2 plug-in.

    Imported lazily so the reconciler doesn't pull in ``alpaca-py`` unless
    someone actually instantiates this class.
    """

    broker: AlpacaBroker
    name: str = "alpaca"

    async def get_account_state(self) -> RawAccountState:
        equity = await self.broker.get_account_equity()
        bp = await self.broker.get_buying_power()
        options_trading_level = await self.broker.get_options_trading_level()
        broker_positions = await self.broker.list_positions()
        positions = tuple(
            PortfolioPosition(
                symbol=p.symbol,
                qty=p.qty,
                avg_entry_price=p.avg_entry_price,
                market_value=p.market_value,
                sector=None,  # resolved in the risk rules via assets.sector_for
                is_option=p.is_option,
                multiplier=p.multiplier,
                unrealized_pl=p.unrealized_pl,
            )
            for p in broker_positions
        )
        # Cash isn't a separate Alpaca call — derive from equity minus market value.
        cash = max(0.0, equity - sum(p.market_value for p in positions))
        return RawAccountState(
            equity=equity,
            cash=cash,
            buying_power=bp,
            open_positions=positions,
            options_trading_level=options_trading_level,
            raw={"source": "alpaca", "equity": equity, "buying_power": bp},
        )
