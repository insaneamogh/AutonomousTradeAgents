"""Simulated broker — fills orders inside the backtester loop.

NOT a ``BrokerInterface`` implementation. The contract is intentionally
different: live brokers expose an async I/O surface; the sim broker fills
deterministically on the next bar. Strategy code is portable because it
emits ``broker.types.OrderRequest`` (the shared input type) — what changes
is which engine consumes those requests.

Phase 0 fill model: market orders fill at the next bar's open ± slippage.
Limit / stop simulation lands in Phase 1.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from broker.types import Order, OrderRequest, OrderStatus, OrderType, Side
from engine.backtester.costs import SecFinraTafCosts, SlippageFn, fixed_bps_slippage
from engine.backtester.events import Bar, FillEvent


@dataclass
class SimulatedBroker:
    """Sim broker for the backtester. Holds a queue of pending orders to fill
    on the next bar's open."""

    cost_model: SecFinraTafCosts = field(default_factory=SecFinraTafCosts)
    slippage_fn: SlippageFn = field(default_factory=lambda: fixed_bps_slippage(2.0))

    _pending: deque[OrderRequest] = field(default_factory=deque, init=False)

    def submit(self, request: OrderRequest) -> None:
        """Queue an order for next-bar fill. Idempotent on ``client_order_id``."""
        if any(r.client_order_id == request.client_order_id for r in self._pending):
            return
        self._pending.append(request)

    def on_bar(self, bar: Bar) -> list[FillEvent]:
        """Fills every queued order at this bar's open price, applies costs.

        Phase 0 assumes the queued orders arrived at the previous bar close,
        so 'next-bar open' is just this bar's open. Phase 1 will track
        order-submission timestamps explicitly.
        """
        fills: list[FillEvent] = []
        while self._pending:
            req = self._pending.popleft()
            if req.symbol != bar.symbol:
                # Multi-symbol Phase 1 will route per-symbol queues. For now,
                # drop mismatched orders rather than silently filling wrong bars.
                continue
            if req.order_type is not OrderType.MARKET:
                # Limit / stop come in Phase 1.
                raise NotImplementedError(
                    f"Sim broker Phase 0 only fills MARKET orders; got {req.order_type}"
                )
            fill_price = self.slippage_fn(req.side, bar.open)
            sec, finra = self.cost_model.fees_for(req.side, req.qty, fill_price)
            order = Order(
                broker_order_id=f"sim-{uuid.uuid4().hex[:12]}",
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                filled_qty=req.qty,
                avg_fill_price=fill_price,
                status=OrderStatus.FILLED,
                submitted_at=bar.timestamp,
                filled_at=bar.timestamp,
                raw={"sim": "true"},
            )
            fills.append(
                FillEvent(
                    order=order,
                    symbol=req.symbol,
                    side=req.side,
                    qty=req.qty,
                    fill_price=fill_price,
                    fill_time=bar.timestamp,
                    sec_fee=sec,
                    finra_taf=finra,
                    slippage_bps=abs(fill_price - bar.open) / bar.open * 10_000.0 if bar.open else 0.0,
                )
            )
        return fills
