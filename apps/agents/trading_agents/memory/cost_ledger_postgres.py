"""PostgresCostLedger — the durable half of the LLM cost ledger.

Migration 0007 created ``llm_calls`` and ``trading_agents.llm`` has always
called ``get_cost_ledger().record(...)`` after every completion. What was
missing was the Postgres implementation, so with ``USE_POSTGRES=1`` the
factory logged a warning and handed back the in-memory ledger. Every cost
row died with the process: the table stayed empty across weeks of real
council runs, and ``/health/full`` reported $0.00 spend no matter how much
had actually been billed.

Cost telemetry is the input to a budget cap, so a ledger that silently
forgets is worse than no ledger — it reads as "we are under budget".

Writes are best-effort at the call site (``llm._record_to_ledger`` wraps
this in a try/except): a ledger outage must never take down a council run.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update

from engine.db import async_session_factory
from engine.db.models import LlmCall
from trading_agents.cost_ledger import LedgerEntry

logger = logging.getLogger("agents.cost.postgres")


def _maybe_uuid(value: str | None) -> uuid.UUID | None:
    """Parse an optional UUID, tolerating the short opaque ids the
    in-memory path uses. A malformed id becomes NULL rather than an error:
    the cost row is still worth keeping without its foreign key."""
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


class PostgresCostLedger:
    """``CostLedger`` backed by the ``llm_calls`` table."""

    def __init__(self) -> None:
        self._session_factory = async_session_factory()

    async def record(self, entry: LedgerEntry) -> LedgerEntry:
        async with self._session_factory() as session:
            row = LlmCall(
                id=uuid.uuid4(),
                agent_decision_id=_maybe_uuid(entry.agent_decision_id),
                user_id=_maybe_uuid(entry.user_id),
                council_run_id=_maybe_uuid(entry.council_run_id),
                model=entry.model,
                role=entry.role,
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cache_read_tokens=entry.cache_read_tokens,
                cache_creation_tokens=entry.cache_creation_tokens,
                cost_usd=Decimal(str(entry.cost_usd)),
                is_mock=entry.is_mock,
                called_at=entry.called_at,
            )
            session.add(row)
            await session.commit()
            entry.id = str(row.id)
        return entry

    async def sum_cost_since(
        self, since: timedelta, *, exclude_mock: bool = True
    ) -> tuple[float, int]:
        cutoff = datetime.now(UTC) - since
        stmt = select(
            func.coalesce(func.sum(LlmCall.cost_usd), 0), func.count(LlmCall.id)
        ).where(LlmCall.called_at >= cutoff)
        if exclude_mock:
            stmt = stmt.where(LlmCall.is_mock.is_(False))
        async with self._session_factory() as session:
            total, count = (await session.execute(stmt)).one()
        return (round(float(total), 6), int(count))

    async def all(self) -> list[LedgerEntry]:
        """Debug / testing only — unbounded scan, never on a hot path."""
        stmt = select(LlmCall).order_by(LlmCall.called_at.desc())
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            LedgerEntry(
                id=str(r.id),
                agent_decision_id=str(r.agent_decision_id) if r.agent_decision_id else None,
                user_id=str(r.user_id) if r.user_id else None,
                council_run_id=str(r.council_run_id) if r.council_run_id else None,
                model=r.model,
                role=r.role,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cache_read_tokens=r.cache_read_tokens,
                cache_creation_tokens=r.cache_creation_tokens,
                cost_usd=float(r.cost_usd),
                is_mock=r.is_mock,
                called_at=r.called_at,
            )
            for r in rows
        ]

    async def backfill_decision_id(
        self, *, council_run_id: str, decision_id: str
    ) -> None:
        """One UPDATE: attach ``decision_id`` to every row from this run that
        isn't already attributed. The ``agent_decision_id IS NULL`` guard
        matters — don't clobber a row some other path already attributed.

        Best-effort, matching ``trading_agents.llm._record_to_ledger``'s
        convention elsewhere in this codebase: a ledger outage must never
        take down a council run.
        """
        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(LlmCall)
                    .where(
                        LlmCall.council_run_id == uuid.UUID(council_run_id),
                        LlmCall.agent_decision_id.is_(None),
                    )
                    .values(agent_decision_id=uuid.UUID(decision_id))
                )
                await session.commit()
        except Exception as exc:
            logger.warning("cost ledger backfill failed (best-effort): %s", exc)
