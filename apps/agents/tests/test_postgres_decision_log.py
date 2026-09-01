"""PostgresDecisionLog.list_pending_reflection — query-shape test.

No live Postgres available in this environment to do a full round-trip
(that lives in ``apps/api/tests/test_postgres_stores.py``, gated behind
``RUN_POSTGRES_TESTS=1`` + a reachable DB). This test instead intercepts
the SQLAlchemy statement right before ``session.execute()`` and asserts
on its compiled shape — which is exactly where the real bug lived: the
query filtered/ordered on the wrong timestamp column.

Real-world context (verified live against production 2026-09-01, via a
read-only query — see fable5findings.md): 6/6 closed decisions had
``triggered_at`` ~117h in the past and ``closed_at`` ~48h in the past.
The OLD query (``AgentDecision.triggered_at >= now() - since``) excludes
every one of those rows once ``since`` is the daily cron's 24h — and can
never re-include them later, because ``triggered_at`` never changes and
only gets further in the past. That is why ``strategy_confidence`` sat at
the migration-0003 seed (0.500) for every strategy: Reflection had never
once fired against real data.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects import postgresql

from trading_agents.memory.decision_log import ALL_USERS
from trading_agents.memory.postgres import PostgresDecisionLog


class _FakeResult:
    """Mimics the ``Result`` shape ``list_pending_reflection`` consumes."""

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return []


class _FakeSession:
    def __init__(self, capture: dict[str, Any]) -> None:
        self._capture = capture

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, stmt: Any) -> _FakeResult:
        # Capture the statement instead of hitting a real DB.
        self._capture["stmt"] = stmt
        return _FakeResult()


class _FakeSessionFactory:
    """Drop-in replacement for the ``async_sessionmaker`` the real class
    stores on ``self._session_factory`` — same call-then-context-manager
    shape (``async with self._session_factory() as session:``)."""

    def __init__(self, capture: dict[str, Any]) -> None:
        self._capture = capture

    def __call__(self) -> _FakeSession:
        return _FakeSession(self._capture)


def _compiled_sql(stmt: Any) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


async def test_list_pending_reflection_filters_on_closed_at_not_triggered_at() -> None:
    """The regression, pinned: the window must be anchored on ``closed_at``
    (when the position actually went flat), not ``triggered_at`` (when the
    entry order was placed) — those can be days apart, and a decision that
    takes longer than ``since`` to close must still be reachable once it
    DOES close, not permanently excluded because its ``triggered_at`` has
    already aged out of the window.

    Revert-checked: pinning the WHERE/ORDER BY back to ``triggered_at``
    makes this fail (compiled SQL contains "triggered_at", not
    "closed_at" as the bound column) — confirmed by hand before this test
    was added, per CLAUDE.md §4.1.
    """
    log = PostgresDecisionLog()
    capture: dict[str, Any] = {}
    log._session_factory = _FakeSessionFactory(capture)  # type: ignore[assignment]

    await log.list_pending_reflection(user_id=ALL_USERS)

    assert "stmt" in capture, "list_pending_reflection never called session.execute()"
    compiled = _compiled_sql(capture["stmt"])

    # ``select(AgentDecision)`` always selects every mapped column, so both
    # "triggered_at" and "closed_at" legitimately appear once each in the
    # SELECT list — that is not the bug. The bug is which column gates the
    # WHERE / ORDER BY, so check those clauses specifically (everything from
    # " FROM " on) rather than whether either name appears anywhere at all.
    rest = compiled.split("FROM agent_decisions", 1)[1]

    assert "agent_decisions.closed_at >=" in rest, (
        f"expected the window to gate on closed_at, got:\n{compiled}"
    )
    assert "agent_decisions.closed_at IS NOT NULL" in rest
    assert "ORDER BY agent_decisions.closed_at" in rest

    assert "agent_decisions.triggered_at >=" not in rest, (
        f"query still gates the window on triggered_at — the exact regression "
        f"this test pins:\n{compiled}"
    )
    assert "ORDER BY agent_decisions.triggered_at" not in rest, (
        f"query still orders by triggered_at — the exact regression this test "
        f"pins:\n{compiled}"
    )
    # realized_pnl / reviewed_at predicates must still be present — the fix
    # is about WHICH timestamp gates the window, not the other conditions.
    assert "realized_pnl IS NOT NULL" in rest
    assert "reviewed_at IS NULL" in rest


async def test_list_pending_reflection_scopes_to_tenant() -> None:
    """A real user id must still add a ``user_id =`` filter — ALL_USERS is
    the only sentinel allowed to skip it (unchanged by this fix, but a
    query-shape rewrite is exactly the kind of change that could silently
    drop a filter, so pin it explicitly)."""
    log = PostgresDecisionLog()
    capture: dict[str, Any] = {}
    log._session_factory = _FakeSessionFactory(capture)  # type: ignore[assignment]

    await log.list_pending_reflection(user_id="00000000-0000-0000-0000-000000000001")

    compiled = _compiled_sql(capture["stmt"])
    assert "agent_decisions.user_id" in compiled
