"""AgentRunRegistry — background council runs + polled progress.

Theater transport decision (see plan): the mobile client cannot consume
SSE without extra machinery (React Native fetch has no ReadableStream),
runs last seconds not minutes, and the deploy is single-instance — so we
run the council in an asyncio task and let the client poll
``GET /agent/run/{id}/progress``. The registry is process-local with a
TTL sweep; the storage surface is small enough that a Redis-backed twin
can replace it if the deploy ever goes multi-instance.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal

from trading_agents.progress import ProgressEvent

logger = logging.getLogger("api.services.agent_runs")

RunStatus = Literal["running", "completed", "failed"]

_TTL = timedelta(minutes=15)
_SWEEP_EVERY = 20  # sweeps amortized over start() calls


@dataclass
class RunRecord:
    run_id: str
    user_id: str
    symbol: str
    status: RunStatus = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    task: asyncio.Task[None] | None = None


class AgentRunRegistry:
    """Process-local registry of in-flight + recent council runs."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._starts_since_sweep = 0

    def _sweep(self) -> None:
        cutoff = datetime.now(timezone.utc) - _TTL
        stale = [rid for rid, r in self._runs.items() if r.created_at < cutoff]
        for rid in stale:
            rec = self._runs.pop(rid)
            if rec.task is not None and not rec.task.done():
                rec.task.cancel()

    def active_run_for(self, user_id: str) -> RunRecord | None:
        for rec in self._runs.values():
            if rec.user_id == user_id and rec.status == "running":
                return rec
        return None

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def start(
        self,
        *,
        user_id: str,
        symbol: str,
        runner: Callable[[Callable[[ProgressEvent], Awaitable[None]]], Awaitable[dict[str, Any]]],
    ) -> RunRecord:
        """Launch a council run in the background.

        ``runner`` receives the progress callback and returns the council
        result dict. One concurrent run per user — a second start returns
        the existing record so a double-tap doesn't double-spend LLM calls.
        """
        self._starts_since_sweep += 1
        if self._starts_since_sweep >= _SWEEP_EVERY:
            self._starts_since_sweep = 0
            self._sweep()

        existing = self.active_run_for(user_id)
        if existing is not None:
            return existing

        rec = RunRecord(run_id=f"run-{uuid.uuid4().hex[:12]}", user_id=user_id, symbol=symbol)
        self._runs[rec.run_id] = rec

        async def _on_event(event: ProgressEvent) -> None:
            rec.events.append(event.to_json())

        async def _drive() -> None:
            try:
                rec.result = await runner(_on_event)
                rec.status = "completed"
            except asyncio.CancelledError:
                rec.status = "failed"
                rec.error = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 — surface to the client, never crash the loop
                logger.exception("council run %s failed", rec.run_id)
                rec.status = "failed"
                rec.error = str(exc)

        rec.task = asyncio.create_task(_drive())
        return rec


_registry: AgentRunRegistry | None = None


def get_run_registry() -> AgentRunRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRunRegistry()
    return _registry


def reset_run_registry_for_tests() -> None:
    global _registry
    _registry = None
