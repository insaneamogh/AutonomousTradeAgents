"""Leader election: one process runs the reconciler fleet and scheduler.

The unit tests drive ``Leadership`` with a scripted lease. The last test
runs ``AdvisoryLease`` against a real Postgres, and only when
LEADER_TEST_DATABASE_URL points at one (advisory locks and pg_locks cannot
be faked meaningfully).
"""

from __future__ import annotations

import os

import pytest

from app.services.platform.leader import AdvisoryLease, Leadership


class _ScriptedLease:
    def __init__(self, acquire: list[object], alive: list[bool] | None = None) -> None:
        self._acquire = list(acquire)
        self._alive = list(alive or [])
        self.released = 0

    async def try_acquire(self) -> bool:
        nxt = self._acquire.pop(0) if self._acquire else False
        if isinstance(nxt, Exception):
            raise nxt
        return bool(nxt)

    async def alive(self) -> bool:
        return self._alive.pop(0) if self._alive else True

    async def release(self) -> None:
        self.released += 1


def _leadership(lease: _ScriptedLease) -> tuple[Leadership, list[str]]:
    events: list[str] = []

    async def elected() -> None:
        events.append("start")

    async def deposed() -> None:
        events.append("stop")

    return Leadership(lease, on_elected=elected, on_deposed=deposed), events


async def test_a_follower_starts_nothing_until_it_wins_the_lease() -> None:
    ld, events = _leadership(_ScriptedLease(acquire=[False, False, True]))
    await ld.tick()
    await ld.tick()
    assert events == [] and not ld.is_leader
    await ld.tick()
    assert events == ["start"] and ld.is_leader
    await ld.tick()  # still leading: no second start
    assert events == ["start"]


async def test_a_lost_lease_stops_the_loops_and_it_can_win_again() -> None:
    ld, events = _leadership(_ScriptedLease(acquire=[True, False, True], alive=[False]))
    await ld.tick()  # elected
    await ld.tick()  # heartbeat: lost
    assert events == ["start", "stop"] and not ld.is_leader
    await ld.tick()  # someone else holds it
    await ld.tick()  # re-elected
    assert events == ["start", "stop", "start"] and ld.is_leader


async def test_a_database_error_leaves_it_a_follower_and_never_raises() -> None:
    ld, events = _leadership(_ScriptedLease(acquire=[ConnectionError("db down"), True]))
    await ld.tick()
    assert events == [] and not ld.is_leader
    await ld.tick()
    assert events == ["start"]


async def test_stop_stops_the_loops_and_releases_the_lease() -> None:
    lease = _ScriptedLease(acquire=[True])
    ld, events = _leadership(lease)
    await ld.tick()
    await ld.stop()
    assert events == ["start", "stop"]
    assert lease.released == 1 and not ld.is_leader


async def test_a_follower_stopping_releases_without_stopping_loops_it_never_ran() -> None:
    lease = _ScriptedLease(acquire=[False])
    ld, events = _leadership(lease)
    await ld.tick()
    await ld.stop()
    assert events == [] and lease.released == 1


_PG = os.environ.get("LEADER_TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG, reason="set LEADER_TEST_DATABASE_URL to a disposable Postgres")
async def test_advisory_lease_against_real_postgres() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    key = 0x41545431  # not the production key, so a shared DB is safe
    e1, e2 = create_async_engine(_PG), create_async_engine(_PG)
    a, b = AdvisoryLease(e1, key), AdvisoryLease(e2, key)
    try:
        assert await a.try_acquire() is True
        assert await b.try_acquire() is False
        assert await a.alive() is True
        await a.release()
        assert await b.try_acquire() is True

        async with e1.connect() as c:
            await c.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_locks "
                     "WHERE locktype = 'advisory' AND objid = :k"), {"k": key},
            )
            await c.commit()
        assert await b.alive() is False
        assert await a.try_acquire() is True
    finally:
        await a.release()
        await b.release()
        await e1.dispose()
        await e2.dispose()
