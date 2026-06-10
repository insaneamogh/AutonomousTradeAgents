"""Wire schema for /api/v1/health/full.

Aggregator response — the mobile Home screen reads this for the
system-status strip. Per-component liveness + a couple of forward-
looking placeholders (LLM cost YTD) that turn into real numbers when
the LiteLLM ledger lands.
"""

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


ComponentStatus = Literal["ok", "warning", "danger", "unknown"]


class ComponentHealth(_Base):
    status: ComponentStatus
    label: str
    """One-line human-readable status (e.g. "Last ran 4m ago", "Halted at -3.2%")."""
    last_event_at: datetime | None = None


class HealthResponse(_Base):
    council: ComponentHealth
    """Last successful /agent/run + count today."""

    approvals: ComponentHealth
    """Pending count + oldest age."""

    broker: ComponentHealth
    """Active broker connection state + last successful broker call."""

    reconciler: ComponentHealth
    """Last reconciler tick + breaker state."""

    llm_cost: ComponentHealth = Field(
        description="Placeholder — turns into a real YTD spend once the LiteLLM ledger ships.",
    )

    generated_at: datetime
