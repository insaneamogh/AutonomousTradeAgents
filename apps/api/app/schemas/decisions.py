"""Decision timeline schemas — the trade biography."""

from __future__ import annotations

from pydantic import Field

from app.schemas.base import CamelCaseModel


class TimelineEventDto(CamelCaseModel):
    kind: str
    at: str | None
    title: str
    detail: str = ""
    data: dict = Field(default_factory=dict)


class DecisionTimelineResponse(CamelCaseModel):
    decision_id: str
    symbol: str
    side: str | None
    status: str
    events: list[TimelineEventDto]
