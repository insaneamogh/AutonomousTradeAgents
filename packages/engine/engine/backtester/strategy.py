"""Strategy protocol.

Every strategy implements one method: ``on_bar(bar) -> list[OrderRequest]``.
Strategies do NOT decide whether they're allowed to trade — that's the risk
engine's job. They produce candidate orders; the engine routes them through
risk → broker.

Reference strategies live under ``engine.backtester.strategies``.
"""

from __future__ import annotations

from typing import Protocol

from broker.types import OrderRequest
from engine.backtester.events import Bar


class Strategy(Protocol):
    name: str

    def on_bar(self, bar: Bar) -> list[OrderRequest]:
        """Called once per bar (close). Returns 0..N order proposals."""
        ...
