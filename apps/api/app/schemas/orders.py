"""Wire schemas for /api/v1/orders."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class _Base(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class OrderResponse(_Base):
    """Mobile-facing order DTO. Mirrors the broker's ``Order`` type but in
    camelCase + with the broker-internal raw dict stripped.
    """

    id: str
    """Our internal orders.id (UUID)."""
    proposal_id: str
    """The agent_decisions/proposal row that originated this order."""
    broker_order_id: str | None
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: int
    """The qty actually submitted (possibly trimmed by the risk re-evaluation)."""
    requested_qty: int = Field(
        description="The qty originally requested by the proposal (pre-trim).",
    )
    order_type: str
    limit_price: float | None
    status: str
    filled_qty: int
    avg_fill_price: float | None
    is_paper: bool
    submitted_at: datetime


class ExecuteResponse(_Base):
    order: OrderResponse | None = None
    risk_blocked: bool
    risk_reason: str
    risk_veto_rule: str | None = None
    informational_flags: list[str] = Field(default_factory=list)
