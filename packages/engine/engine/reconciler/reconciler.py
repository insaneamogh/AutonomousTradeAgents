"""Reconciler — the periodic asyncio loop.

One pass per ``interval_seconds``:
    1. Poll the broker for current account state.
    2. Write a ``positions_snapshot`` row (computes daily_pnl against the
       first snapshot of today).
    3. Evaluate the circuit breaker — trip to ``halted`` if the daily drawdown
       breached the threshold.

The loop swallows per-tick exceptions and keeps running — a transient broker
failure shouldn't take the reconciler down. Fatal errors surface in logs.

Wiring options (see AGENTV1 §Next-session Step 4):
  - FastAPI lifespan background task (single-replica Fly deploys — Phase 0/1)
  - Separate ``apps/reconciler`` service (multi-replica — Phase 2)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.reconciler.breaker import BreakerTransition, evaluate_breaker
from engine.reconciler.snapshot import write_snapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from engine.reconciler.poller import BrokerPoller

logger = logging.getLogger("engine.reconciler")


@dataclass(frozen=True)
class ReconcilerConfig:
    interval_seconds: float = 30.0
    halt_threshold_pct: float = -3.0
    # When tick() raises an exception, log and continue. Set False for tests
    # that want exceptions to surface.
    swallow_errors: bool = True


@dataclass(frozen=True)
class ReconcilerTickResult:
    snapshot_id: str
    transition: BreakerTransition


class Reconciler:
    def __init__(
        self,
        *,
        poller: "BrokerPoller",
        session_factory: "async_sessionmaker",
        user_id: uuid.UUID,
        config: ReconcilerConfig | None = None,
    ) -> None:
        self._poller = poller
        self._session_factory = session_factory
        self._user_id = user_id
        self._config = config or ReconcilerConfig()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def config(self) -> ReconcilerConfig:
        return self._config

    async def tick(self) -> ReconcilerTickResult:
        """Run one reconcile pass. Testable in isolation."""
        state = await self._poller.get_account_state()
        async with self._session_factory() as session:
            snapshot = await write_snapshot(
                session,
                user_id=self._user_id,
                state=state,
                source=self._poller.name,
            )
            transition = await evaluate_breaker(
                session,
                user_id=self._user_id,
                snapshot=snapshot,
                threshold_pct=self._config.halt_threshold_pct,
            )
        return ReconcilerTickResult(snapshot_id=str(snapshot.id), transition=transition)

    async def run_forever(self) -> None:
        logger.info(
            "reconciler starting — interval=%ss, halt_threshold=%s%%, user=%s",
            self._config.interval_seconds, self._config.halt_threshold_pct, self._user_id,
        )
        while not self._stop.is_set():
            try:
                result = await self.tick()
                if result.transition.tripped:
                    logger.warning(
                        "breaker tripped: %s → %s (%s)",
                        result.transition.previous_status,
                        result.transition.new_status,
                        result.transition.reason,
                    )
            except Exception:  # noqa: BLE001
                if self._config.swallow_errors:
                    logger.exception("reconciler tick failed — continuing")
                else:
                    raise
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._config.interval_seconds)
            except asyncio.TimeoutError:
                pass
        logger.info("reconciler stopped")

    def start(self) -> None:
        """Spawn the run_forever loop as a background task."""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.run_forever(), name="reconciler")

    async def stop(self) -> None:
        """Signal stop + wait for the task to drain."""
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:  # noqa: BLE001
                logger.exception("reconciler task raised on shutdown")
        self._task = None
