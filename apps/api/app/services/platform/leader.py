"""One process runs the background loops. Every process serves HTTP.

docs/PLAN_PLATFORM.md Phase 6. The reconciler fleet (fills, stops, the
exit ladder, the expiry sweep) and the council scheduler (sweeps, IV
snapshot, EOD) start in the FastAPI lifespan, so they start once per
process. Two processes means two of each: two exit ladders racing to close
the same position, two council sweeps spending twice. That happens today
with ``UVICORN_WORKERS`` above 1, and for a few seconds on every Railway
deploy while the old container drains and the new one is already up.

The lease is a Postgres session-level advisory lock, held on one dedicated
AUTOCOMMIT connection for as long as this process leads. Postgres releases
it the moment that connection dies, whether by shutdown, crash or network
loss, so there is no expiry to tune and no stale leader to clean up. The
heartbeat checks ``pg_locks`` for the lock on this backend, not just that
the connection answers, so a connection that silently lost its session
(a proxy in transaction-pooling mode, for one) is not mistaken for a lease.

When the lease connection drops, Postgres frees the lock at once, but
this process only notices on its next heartbeat. So up to one heartbeat
interval of overlap is possible in that case. The order paths keep their
own idempotency (client_order_id, the in-flight check) for that window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger("api.leader")

# b"ATA1". Any constant works; it only has to be the same in every process
# and unused by anything else. Below 2**31, so pg_locks reports it as
# classid 0, objid LEADER_LOCK_KEY.
LEADER_LOCK_KEY = 0x41544131
DEFAULT_HEARTBEAT_S = 15.0

_HOLDS_LOCK = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
    "AND pid = pg_backend_pid() AND classid = 0 AND objid = :k "
    "AND objsubid = 1 AND granted)"
)


class Lease(Protocol):
    async def try_acquire(self) -> bool: ...
    async def alive(self) -> bool: ...
    async def release(self) -> None: ...


class AdvisoryLease:
    """``pg_try_advisory_lock`` on a connection kept out of the pool."""

    def __init__(self, engine: AsyncEngine, key: int = LEADER_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._conn: AsyncConnection | None = None

    async def try_acquire(self) -> bool:
        if self._conn is not None:
            return True
        conn = await self._engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = bool(
                (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key}))
                .scalar()
            )
        except Exception:
            await self._discard(conn)
            raise
        if not got:
            await conn.close()
            return False
        self._conn = conn
        return True

    async def alive(self) -> bool:
        if self._conn is None:
            return False
        try:
            held = bool((await self._conn.execute(_HOLDS_LOCK, {"k": self._key})).scalar())
        except Exception:
            logger.warning("leader: heartbeat query failed; treating the lease as lost",
                           exc_info=True)
            held = False
        if not held:
            conn, self._conn = self._conn, None
            await self._discard(conn)
        return held

    async def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
        await self._discard(conn)

    @staticmethod
    async def _discard(conn: AsyncConnection) -> None:
        """Close the DBAPI connection rather than return it to the pool: a
        pooled connection still holding the session lock would keep this
        process "leading" through whatever checks it out next."""
        with contextlib.suppress(Exception):
            await conn.invalidate()
        with contextlib.suppress(Exception):
            await conn.close()


class Leadership:
    """Starts the loops when this process takes the lease, stops them when
    it loses it, and keeps trying while it is a follower."""

    def __init__(
        self,
        lease: Lease,
        *,
        on_elected: Callable[[], Awaitable[None]],
        on_deposed: Callable[[], Awaitable[None]],
        interval_s: float = DEFAULT_HEARTBEAT_S,
    ) -> None:
        self._lease = lease
        self._on_elected = on_elected
        self._on_deposed = on_deposed
        self._interval_s = interval_s
        self._leading = False
        self._task: asyncio.Task[None] | None = None

    @property
    def is_leader(self) -> bool:
        return self._leading

    async def tick(self) -> None:
        """One election step. Never raises."""
        try:
            if not self._leading:
                if await self._lease.try_acquire():
                    self._leading = True
                    logger.warning("leader: this process now runs the background loops")
                    await self._on_elected()
            elif not await self._lease.alive():
                self._leading = False
                logger.error("leader: lease LOST; stopping the background loops")
                await self._on_deposed()
        except Exception:
            logger.exception("leader: election step failed; retrying next interval")

    async def _run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self._interval_s)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="leader-election")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._leading:
            self._leading = False
            try:
                await self._on_deposed()
            except Exception:
                logger.exception("leader: stopping the loops on shutdown failed")
        await self._lease.release()


_current: Leadership | None = None


def set_current_leadership(leadership: Leadership | None) -> None:
    global _current
    _current = leadership


def current_leadership() -> Leadership | None:
    """For health reporting. None when election is not in use."""
    return _current
