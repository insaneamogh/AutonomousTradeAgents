"""Wire schema for /api/v1/circuit-breaker — the drawdown halt banner."""

from __future__ import annotations

from datetime import datetime

from app.schemas.base import CamelCaseModel


class CircuitBreakerResponse(CamelCaseModel):
    halted: bool
    reason: str | None = None
    halted_at: datetime | None = None
    observed_drawdown_pct: float | None = None
    threshold_pct: float | None = None
