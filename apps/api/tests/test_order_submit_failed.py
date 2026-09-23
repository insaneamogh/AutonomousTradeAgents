"""mark_order_submit_failed may only touch a row the broker never saw."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql


async def test_it_only_moves_a_pending_row_with_no_broker_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.db.session as session_mod
    from app.services.orders.order_store import mark_order_submit_failed

    captured: list[Any] = []

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def execute(self, stmt: Any) -> None:
            captured.append(stmt)

        async def commit(self) -> None:
            return None

    monkeypatch.setenv("USE_POSTGRES", "1")
    monkeypatch.setattr(session_mod, "async_session_factory", lambda: (lambda: _Session()))
    await mark_order_submit_failed(uuid.uuid4())

    sql = str(captured[0].compile(dialect=postgresql.dialect(),
                                  compile_kwargs={"literal_binds": True}))
    assert "UPDATE orders SET status='rejected'" in sql
    assert "orders.status = 'pending'" in sql
    assert "orders.broker_order_id IS NULL" in sql


async def test_no_row_id_or_no_postgres_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.orders.order_store import mark_order_submit_failed

    monkeypatch.delenv("USE_POSTGRES", raising=False)
    await mark_order_submit_failed(uuid.uuid4())  # must not touch a DB
    monkeypatch.setenv("USE_POSTGRES", "1")
    await mark_order_submit_failed(None)
