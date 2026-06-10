"""ReviewStore — operator-graded decision reviews.

Protocol + InMemoryReviewStore + (deferred) PostgresReviewStore. Same
pattern as the other Phase 3/4 stores: the API picks the impl via
``USE_POSTGRES`` env, defaulting to in-memory.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

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
    reviewed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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
        now = datetime.now(timezone.utc)
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
# Factory
# ─────────────────────────────────────────────────────────────────────


_review_store: ReviewStore | None = None


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def get_review_store() -> ReviewStore:
    """Process singleton. Postgres impl deferred — InMemory is the live
    default during the Phase 4 month-1 review window.
    """
    import logging

    global _review_store
    if _review_store is None:
        if _is_truthy(os.environ.get("USE_POSTGRES")):
            logging.getLogger("api.review").warning(
                "USE_POSTGRES=1 but PostgresReviewStore is not yet wired — "
                "falling back to InMemoryReviewStore. Reviews won't persist."
            )
        _review_store = InMemoryReviewStore()
    return _review_store


def reset_review_store_for_tests() -> None:
    global _review_store
    _review_store = None
