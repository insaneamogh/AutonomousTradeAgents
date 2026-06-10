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
    name: str = "mock"

    async def get_account_state(self) -> RawAccountState:
        return RawAccountState(
            equity=self.equity,
            cash=self.cash,
            buying_power=self.buying_power,
            open_positions=tuple(self.positions),
            raw={"source": "mock", "equity": self.equity},
        )


@dataclass
class AlpacaBrokerPoller:
    """Adapter over the existing AlpacaBroker. Phase 2 plug-in.

    Imported lazily so the reconciler doesn't pull in ``alpaca-py`` unless
    someone actually instantiates this class.
    """

    broker: "AlpacaBroker"
    name: str = "alpaca"

    async def get_account_state(self) -> RawAccountState:
        equity = await self.broker.get_account_equity()
        bp = await self.broker.get_buying_power()
        broker_positions = await self.broker.list_positions()
        positions = tuple(
            PortfolioPosition(
                symbol=p.symbol,
                qty=p.qty,
                avg_entry_price=p.avg_entry_price,
                market_value=p.market_value,
                sector=None,  # resolved in the risk rules via assets.sector_for
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
            raw={"source": "alpaca", "equity": equity, "buying_power": bp},
        )
