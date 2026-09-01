"""Swapping the Alpaca keys under a user must not inherit the old
account's state.

Everything derived is keyed on `user_id`, not on the broker account. On a
fresh $100k paper account that means: a drawdown HALT raised by the old
account (which never auto-clears), open decisions for positions the new
account does not hold, and `orders` rows whose `broker_order_id` now 404s.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.services.orders.account_switch import reconcile_account_identity


def _session(stored: str | None, *, retired: int = 0, canceled: int = 0, halted: int = 0):
    """Sequential `execute` results: stored-number read, then the three
    retirement statements in the order the function issues them."""
    session = MagicMock()

    first = MagicMock()
    first.scalar_one_or_none = MagicMock(return_value=stored)

    def _rows(n: int) -> MagicMock:
        r = MagicMock()
        r.all = MagicMock(return_value=[object()] * n)
        return r

    session.execute = AsyncMock(
        side_effect=[first, MagicMock(), _rows(retired), _rows(canceled), _rows(halted)]
    )
    return session


async def _run(stored: str | None, observed: str | None, **counts: Any) -> tuple[bool, MagicMock]:
    session = _session(stored, **counts)
    switched = await reconcile_account_identity(
        session,
        user_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        observed_account_number=observed,
    )
    return switched, session


async def test_a_changed_account_number_is_a_switch() -> None:
    switched, _ = await _run("PA3IAZI74E5R", "PA9NEWACCT01", retired=8, canceled=3, halted=1)
    assert switched is True


async def test_the_first_observation_is_a_backfill_not_a_switch() -> None:
    """Every connection in the database has a NULL account_number until
    this code first runs. Treating that as a switch would wipe a
    perfectly good live book on deploy."""
    switched, session = await _run(None, "PA3IAZI74E5R")

    assert switched is False
    # Exactly two statements: read the stored value, write it back. No
    # retirement statements issued at all.
    assert session.execute.await_count == 2


async def test_an_unchanged_account_does_nothing() -> None:
    switched, session = await _run("PA3IAZI74E5R", "PA3IAZI74E5R")

    assert switched is False
    assert session.execute.await_count == 1


async def test_a_broker_that_reports_no_account_number_is_not_a_switch() -> None:
    """Never infer a swap from missing data — that would retire a live
    book every time the account endpoint omits the field."""
    switched, session = await _run("PA3IAZI74E5R", None)

    assert switched is False
    session.execute.assert_not_awaited()


async def test_the_switch_clears_a_halt_the_old_account_raised() -> None:
    """The breaker deliberately never auto-unhalts — a halt is a statement
    about one account's equity curve. That is exactly why it must not
    survive a switch: the curve it described no longer exists, and the new
    account would be frozen from its first tick with no visible cause."""
    _, session = await _run("OLD", "NEW", retired=0, canceled=0, halted=1)

    stmts = [str(c.args[0]) for c in session.execute.await_args_list]
    assert any("circuit_breaker_state" in s for s in stmts)


async def test_nothing_is_deleted_only_stamped() -> None:
    """The Refusal Ledger is built from agent_decisions; the audit chain
    has to survive a key change."""
    _, session = await _run("OLD", "NEW", retired=8, canceled=3, halted=1)

    stmts = [str(c.args[0]) for c in session.execute.await_args_list]
    assert not any(s.strip().upper().startswith("DELETE") for s in stmts)
    assert any("UPDATE agent_decisions" in s for s in stmts)
