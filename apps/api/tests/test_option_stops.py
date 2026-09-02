"""option_stops.py — the resting broker-side protective stop.

``_resting_stop_row`` is the one query this module runs before every
place-or-replace decision. It went live for the first time on 2026-09-02
(the first option fill against a real broker+DB) and raised
``AttributeError: type object 'Order' has no attribute 'created_at'`` on
every single call — the query referenced a column that does not exist on
the model (``submitted_at`` does; ``created_at`` never did). Caught by no
existing test because the only prior coverage
(``test_position_manager.py``'s in-flight-close guard test) mocks the
session wholesale and never touches a real ``Order`` attribute.

The regression here needs no DB connection to catch: referencing a
nonexistent column on a SQLAlchemy declarative class raises AttributeError
the moment the ``select(...)`` statement is BUILT, before ``session.execute``
is ever called — so a mocked session still exercises the exact line that
broke.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.services.orders.option_stops import _resting_stop_row


async def test_resting_stop_row_builds_its_query_without_raising() -> None:
    """Regression test for the 2026-09-02 live AttributeError — see module
    docstring. Building the query is enough to reproduce it; the mocked
    session's return value only needs to support ``.scalars().first()``."""
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=result)

    row = await _resting_stop_row(session, uuid.uuid4())

    assert row is None
    session.execute.assert_awaited_once()


async def test_resting_stop_row_orders_by_a_real_column() -> None:
    """Pin the specific column, not just "doesn't raise" — asserted on the
    compiled SQL so a future revert to ``created_at`` (a plausible-looking
    but nonexistent column) is caught even if it somehow stopped raising."""
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=result)

    await _resting_stop_row(session, uuid.uuid4())

    stmt = session.execute.await_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "submitted_at" in sql
    assert "created_at" not in sql
