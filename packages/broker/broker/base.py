"""The broker interface — every broker implementation must satisfy this.

Implementations: ``broker.alpaca.AlpacaBroker`` (US, paper + live) and
``broker.zerodha.ZerodhaBroker`` (India, live only — Kite has no paper env).
Next: IBKR.

Architecture rule: nothing in the agent council (`apps/agents`) calls this
directly. Calls flow through ``packages/engine/risk`` → here.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from broker.types import Order, OrderRequest, Position


@runtime_checkable
class BrokerInterface(Protocol):
    """The contract. Use `@runtime_checkable` so mocks/stubs satisfy it without inheritance."""

    name: str
    is_paper: bool

    async def place_order(self, request: OrderRequest) -> Order:
        """Submit an order. Must be idempotent on `client_order_id`."""
        ...

    async def cancel_order(self, broker_order_id: str) -> Order:
        """Best-effort cancel. Returns the (possibly final) order state."""
        ...

    async def get_order(self, broker_order_id: str) -> Order:
        """Fetch current state for a single order."""
        ...

    async def cancel_open_orders(self, symbol: str) -> int:
        """Cancel every open order on a symbol (e.g. resting bracket
        children before an early close). Returns how many were canceled."""
        ...

    async def list_positions(self) -> list[Position]:
        """All open positions on the account."""
        ...

    async def get_position(self, symbol: str) -> Position | None:
        """Single-symbol position, or None if flat."""
        ...

    async def get_account_equity(self) -> float:
        """Total account equity in the account's native currency
        (USD for Alpaca, INR for Zerodha). Risk works in ratios.
        """
        ...

    async def get_buying_power(self) -> float:
        """Amount currently available to buy, native currency.
        Pre-trade risk uses this.
        """
        ...
