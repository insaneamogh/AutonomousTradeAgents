"""Broker abstraction package. Implemented: Alpaca + Zerodha (Kite Connect).

Concrete broker implementations (``broker.alpaca``, ``broker.zerodha``) are
NOT imported eagerly — callers must do ``from broker.alpaca import
AlpacaBroker`` / ``from broker.zerodha import ZerodhaBroker`` explicitly.
This keeps the backtester (which only needs ``broker.types``) from pulling
in the ``alpaca-py`` SDK or ``httpx`` as transitive imports.
"""

from broker.base import BrokerInterface
from broker.types import (
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
)

__all__ = [
    "BrokerInterface",
    "Order",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Position",
    "Side",
    "TimeInForce",
]
