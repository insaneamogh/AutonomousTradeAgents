"""Wire schema for GET /api/v1/agent/autonomy — the readiness report."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.base import CamelCaseModel


class AutonomyCheck(CamelCaseModel):
    id: str
    label: str
    ok: bool
    severity: Literal["blocker", "warning", "info"]
    detail: str
    fix: str | None = None


class AutonomyReport(CamelCaseModel):
    state: Literal["running", "idle", "blocked"]
    headline: str
    checks: list[AutonomyCheck]
    last_sweep_at: datetime | None = None
    next_sweep_at: datetime | None = None
    as_of: datetime
