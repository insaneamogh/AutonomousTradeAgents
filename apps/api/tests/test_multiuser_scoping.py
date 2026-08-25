"""Multi-user scoping — the executor must thread the caller's user_id into
every store read/write so one user can't touch another's proposal.

MockStore/PostgresStore behavior is covered elsewhere; here we use a
capturing fake store to pin that ``execute_proposal`` passes ``user_id``
to BOTH ``list_pending`` (the ownership lookup) and ``decide`` (the
state write). The real per-user filtering lives in PostgresStore._uid +
its WHERE clauses (Postgres-marked integration, not in CI).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.schemas.approvals import ApprovalProposalDto, DecisionResponse  # noqa: E402
from app.services.council.postgres_store import DEFAULT_USER_ID, _uid  # noqa: E402
from app.services.orders.executor import execute_proposal  # noqa: E402

USER_A = "11111111-1111-1111-1111-111111111111"


def _proposal(pid: str = "agent-x", symbol: str = "NVDA") -> ApprovalProposalDto:
    return ApprovalProposalDto(
        id=pid,
        symbol=symbol,
        side="BUY",
        qty=10,
        order_type="MARKET",
        estimated_notional=1000.0,
        rationale="t",
        bull_case="t",
        bear_case="t",
        risk_level=2,
        conviction_level=4,
        proposed_at=datetime.now(timezone.utc),
    )


class _CapturingStore:
    """Records the user_id passed to the user-scoped methods."""

    def __init__(self, proposal: ApprovalProposalDto) -> None:
        self._proposal = proposal
        self.list_pending_user_ids: list[str | None] = []
        self.decide_user_ids: list[str | None] = []

    async def list_pending(self, user_id: str | None = None) -> list[ApprovalProposalDto]:
        self.list_pending_user_ids.append(user_id)
        return [self._proposal]

    async def decide(self, proposal_id, outcome, *, user_id=None, exit_mode=None):  # noqa: ANN001
        self.decide_user_ids.append(user_id)
        return DecisionResponse(
            proposal_id=proposal_id, outcome=outcome, decided_at=datetime.now(timezone.utc)
        )


@pytest.fixture(autouse=True)
def _paper_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "paper")  # in-memory paper fill, no broker
    monkeypatch.delenv("USE_POSTGRES", raising=False)


async def test_executor_threads_user_id_into_store_calls() -> None:
    proposal = _proposal()
    store = _CapturingStore(proposal)

    await execute_proposal(user_id=USER_A, proposal_id=proposal.id, store=store)

    # The ownership lookup AND the state write both carried the caller's id.
    assert store.list_pending_user_ids == [USER_A]
    assert store.decide_user_ids == [USER_A]


def test_uid_resolution() -> None:
    assert _uid(None) == DEFAULT_USER_ID  # cron/legacy fallback
    assert str(_uid(USER_A)) == USER_A  # real user passes through
    assert _uid("not-a-uuid") == DEFAULT_USER_ID  # garbage → safe fallback
