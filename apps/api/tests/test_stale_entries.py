"""The pre-open entry revalidation.

Answers a question nothing in the codebase did: an approved equity entry
goes to the broker GTC when it carries a bracket, so a proposal drafted on
Monday's close could fill on Wednesday on a thesis nothing had re-examined
since. These pin the boundary that decides "stale".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.orders import stale_entries as mod

# 2026-09-01 is a Tuesday; the regular session is 13:30-20:00 UTC.
DURING_SESSION = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
BEFORE_OPEN = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
YESTERDAY = datetime(2026, 8, 31, 19, 0, tzinfo=UTC)


def _order(**kw: Any) -> SimpleNamespace:
    base = SimpleNamespace(
        id=uuid.uuid4(),
        client_order_id="agent-x",
        broker_order_id="brk-1",
        symbol="AAPL",
        side="BUY",
        qty=15,
        filled_qty=0,
        status="accepted",
        submitted_at=YESTERDAY,
        canceled_at=None,
        rejected_reason=None,
    )
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def _decision(side: str = "BUY") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), proposal={"side": side})


def _harness(pairs: list[tuple[Any, Any]]) -> tuple[MagicMock, MagicMock, Any]:
    broker = MagicMock()
    broker.cancel_order = AsyncMock()
    session = MagicMock()
    result = MagicMock()
    result.all = MagicMock(return_value=pairs)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    class _Ctx:
        async def __aenter__(self) -> Any:
            return (broker, SimpleNamespace(is_paper=True))

        async def __aexit__(self, *a: Any) -> bool:
            return False

    class _SessionCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *a: Any) -> bool:
            return False

    return broker, session, (lambda: _SessionCtx(), _Ctx)


async def _run(pairs: list[tuple[Any, Any]], now: datetime) -> tuple[int, MagicMock, MagicMock]:
    broker, session, (factory, ctx) = _harness(pairs)
    with patch.object(mod, "with_broker_client", lambda *a, **k: ctx()):
        n = await mod.sweep_stale_entry_orders_for_user(
            user_id=str(uuid.uuid4()), session_factory=factory, now=now
        )
    return n, broker, session


async def test_cancels_an_entry_order_that_predates_this_sessions_open() -> None:
    order = _order()
    n, broker, _ = await _run([(order, _decision("BUY"))], DURING_SESSION)

    assert n == 1
    broker.cancel_order.assert_awaited_once_with("brk-1")
    assert order.status == "canceled"
    assert "stale_entry_thesis" in order.rejected_reason


async def test_never_sweeps_before_the_session_has_opened() -> None:
    """The boundary is "did this order survive an open". Pre-market, that
    question has no answer yet — and sweeping then would cancel every
    order queued FOR the open, seconds before it would have filled."""
    n, broker, _ = await _run([(_order(), _decision("BUY"))], BEFORE_OPEN)

    assert n == 0
    broker.cancel_order.assert_not_awaited()


async def test_never_cancels_an_exit_order() -> None:
    """A SELL against a long-entry decision is an exit — a bracket child,
    a stop, a close. Cancelling one strips the protection off a live
    position, the exact opposite of the intent."""
    n, broker, _ = await _run([(_order(side="SELL"), _decision("BUY"))], DURING_SESSION)

    assert n == 0
    broker.cancel_order.assert_not_awaited()


async def test_a_shorts_entry_sell_is_still_an_entry() -> None:
    """A short's entry IS a SELL. Assuming entries are BUYs would leave
    exactly the orders this exists to cancel untouched."""
    n, broker, _ = await _run([(_order(side="SELL"), _decision("SELL"))], DURING_SESSION)

    assert n == 1
    broker.cancel_order.assert_awaited_once()


async def test_a_failed_cancel_leaves_the_row_working_rather_than_lying() -> None:
    order = _order()
    broker, session, (factory, ctx) = _harness([(order, _decision("BUY"))])
    broker.cancel_order = AsyncMock(side_effect=RuntimeError("alpaca down"))
    with patch.object(mod, "with_broker_client", lambda *a, **k: ctx()):
        n = await mod.sweep_stale_entry_orders_for_user(
            user_id=str(uuid.uuid4()), session_factory=factory, now=DURING_SESSION
        )

    assert n == 0
    assert order.status == "accepted"
    session.commit.assert_not_awaited()
