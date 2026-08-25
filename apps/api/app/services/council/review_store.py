"""ReviewStore — operator-graded decision reviews.

Protocol + InMemoryReviewStore + (deferred) PostgresReviewStore. Same
pattern as the other Phase 3/4 stores: the API picks the impl via
``USE_POSTGRES`` env, defaulting to in-memory.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from app.core.time import utc_now
from engine.env import env_flag

Grade = Literal["good", "bad", "skip"]
"""Operator's verdict on a completed decision.

  - ``good``  — the agent's call was right (independent of PnL sign).
  - ``bad``   — the agent's call was wrong (also independent of PnL).
  - ``skip``  — the trade is ambiguous / hard to grade; don't count it
    in the agreement stat.
"""


@dataclass
class DecisionReviewRecord:
    id: str
    decision_id: str
    operator_user_id: str
    grade: Grade
    notes: str | None = None
    reviewed_at: datetime = field(default_factory=lambda: utc_now())


@runtime_checkable
class ReviewStore(Protocol):
    async def upsert_review(
        self,
        *,
        decision_id: str,
        operator_user_id: str,
        grade: Grade,
        notes: str | None = None,
    ) -> DecisionReviewRecord: ...

    async def get_review_by_decision_and_operator(
        self,
        *,
        decision_id: str,
        operator_user_id: str,
    ) -> DecisionReviewRecord | None: ...

    async def list_reviews_for_operator(
        self,
        operator_user_id: str,
    ) -> list[DecisionReviewRecord]: ...


# ─────────────────────────────────────────────────────────────────────
# In-memory impl
# ─────────────────────────────────────────────────────────────────────


class InMemoryReviewStore:
    """Default in-memory backing. UQ on (decision_id, operator_user_id)
    mirrors the migration 0006 constraint.
    """

    def __init__(self) -> None:
        self._rows: dict[str, DecisionReviewRecord] = {}

    def _find(
        self, decision_id: str, operator_user_id: str
    ) -> DecisionReviewRecord | None:
        for r in self._rows.values():
            if r.decision_id == decision_id and r.operator_user_id == operator_user_id:
                return r
        return None

    async def upsert_review(
        self,
        *,
        decision_id: str,
        operator_user_id: str,
        grade: Grade,
        notes: str | None = None,
    ) -> DecisionReviewRecord:
        existing = self._find(decision_id, operator_user_id)
        now = utc_now()
        if existing is not None:
            existing.grade = grade
            existing.notes = notes
            existing.reviewed_at = now
            return existing
        rec = DecisionReviewRecord(
            id=str(uuid.uuid4()),
            decision_id=decision_id,
            operator_user_id=operator_user_id,
            grade=grade,
            notes=notes,
            reviewed_at=now,
        )
        self._rows[rec.id] = rec
        return rec

    async def get_review_by_decision_and_operator(
        self,
        *,
        decision_id: str,
        operator_user_id: str,
    ) -> DecisionReviewRecord | None:
        return self._find(decision_id, operator_user_id)

    async def list_reviews_for_operator(
        self,
        operator_user_id: str,
    ) -> list[DecisionReviewRecord]:
        return [
            r for r in self._rows.values() if r.operator_user_id == operator_user_id
        ]




# ─────────────────────────────────────────────────────────────────────
# Postgres impl — migration 0006's decision_review table
# ─────────────────────────────────────────────────────────────────────


class PostgresReviewStore:
    """SQLAlchemy-backed ReviewStore. Upsert leans on the
    (decision_id, operator_user_id) unique constraint so concurrent
    grades of the same decision can't produce duplicates.
    """

    def __init__(self) -> None:
        from engine.db.session import async_session_factory

        self._session_factory = async_session_factory()

    @staticmethod
    def _to_record(row) -> DecisionReviewRecord:  # noqa: ANN001 — SQLAlchemy row
        return DecisionReviewRecord(
            id=str(row.id),
            decision_id=str(row.decision_id),
            operator_user_id=str(row.operator_user_id),
            grade=row.grade,
            notes=row.notes,
            reviewed_at=row.reviewed_at,
        )

    async def upsert_review(
        self,
        *,
        decision_id: str,
        operator_user_id: str,
        grade: Grade,
        notes: str | None = None,
    ) -> DecisionReviewRecord:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from engine.db.models import DecisionReview

        now = utc_now()
        async with self._session_factory() as session:
            stmt = (
                pg_insert(DecisionReview)
                .values(
                    id=uuid.uuid4(),
                    decision_id=uuid.UUID(decision_id),
                    operator_user_id=uuid.UUID(operator_user_id),
                    grade=grade,
                    notes=notes,
                    reviewed_at=now,
                )
                .on_conflict_do_update(
                    constraint="uq_decision_review_decision_operator",
                    set_={"grade": grade, "notes": notes, "reviewed_at": now},
                )
            )
            await session.execute(stmt)
            await session.commit()

            row = (
                await session.execute(
                    select(DecisionReview).where(
                        DecisionReview.decision_id == uuid.UUID(decision_id),
                        DecisionReview.operator_user_id == uuid.UUID(operator_user_id),
                    )
                )
            ).scalar_one()
        return self._to_record(row)

    async def get_review_by_decision_and_operator(
        self,
        *,
        decision_id: str,
        operator_user_id: str,
    ) -> DecisionReviewRecord | None:
        from sqlalchemy import select

        from engine.db.models import DecisionReview

        try:
            did = uuid.UUID(decision_id)
            oid = uuid.UUID(operator_user_id)
        except (ValueError, TypeError):
            return None
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(DecisionReview).where(
                        DecisionReview.decision_id == did,
                        DecisionReview.operator_user_id == oid,
                    )
                )
            ).scalar_one_or_none()
        return self._to_record(row) if row is not None else None

    async def list_reviews_for_operator(
        self,
        operator_user_id: str,
    ) -> list[DecisionReviewRecord]:
        from sqlalchemy import select

        from engine.db.models import DecisionReview

        try:
            oid = uuid.UUID(operator_user_id)
        except (ValueError, TypeError):
            return []
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(DecisionReview).where(
                            DecisionReview.operator_user_id == oid
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [self._to_record(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


_review_store: ReviewStore | None = None


def get_review_store() -> ReviewStore:
    """Process singleton. Postgres when USE_POSTGRES=1, else in-memory."""
    global _review_store
    if _review_store is None:
        if env_flag("USE_POSTGRES"):
            _review_store = PostgresReviewStore()
        else:
            _review_store = InMemoryReviewStore()
    return _review_store


def reset_review_store_for_tests() -> None:
    global _review_store
    _review_store = None
