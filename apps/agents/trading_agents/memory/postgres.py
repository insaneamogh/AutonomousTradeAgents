"""PostgresDecisionLog + PostgresStrategyConfidenceStore — Reflection backing.

Wired against migrations 0001 (agent_decisions base) + 0003 (Reflection
extension columns + strategy_confidence). The Postgres impls satisfy the
``DecisionLog`` + ``StrategyConfidenceStore`` Protocols from
``trading_agents.memory``; the council + reflection node don't care
which one they got.

The ``user_id`` column is NOT NULL on the table — when ``run_council``
runs without a real user (CLI smoke), we use the fixture user id that
``apps/api/PostgresAuthStore`` seeds. Reflection CLI accepts a
``--user-id`` flag in a follow-on; for now it defaults to the fixture.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from engine.db import async_session_factory
from engine.db.models import AgentDecision, StrategyConfidence

from trading_agents.memory.decision_log import DecisionEntry
from trading_agents.memory.strategy_confidence import (
    MAX_CONFIDENCE,
    MAX_CONFIDENCE_DELTA_PER_CYCLE,
    MIN_CONFIDENCE,
    StrategyConfidenceRow,
)
from trading_agents.strategies import STRATEGY_REGISTRY

logger = logging.getLogger("agents.memory.postgres")


# Matches PostgresAuthStore's FIXTURE_USER_ID + migration 0001 seed.
# Council runs without a real user_id resolve here; production runs
# (from /api/v1/agent/run) carry the real user.id from the auth gate.
FIXTURE_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_entry(r: AgentDecision) -> DecisionEntry:
    return DecisionEntry(
        id=str(r.id),
        user_id=str(r.user_id) if r.user_id else None,
        symbol=r.symbol,
        horizon=r.horizon,
        triggered_at=r.triggered_at,
        regime=r.regime,
        selected_strategy=r.selected_strategy,
        selector_confidence=float(r.selector_confidence),
        selector_rationale=r.selector_rationale,
        final_action=r.final_action,
        proposal_id=(r.proposal or {}).get("id") if r.proposal else None,
        risk_approved=bool(r.risk_approved),
        risk_veto_rule=r.risk_veto_rule,
        technical_score=float(r.technical_score) if r.technical_score is not None else None,
        fundamental_score=float(r.fundamental_score) if r.fundamental_score is not None else None,
        macro_score=float(r.macro_score) if r.macro_score is not None else None,
        raw_state=r.proposal or {},
        fill_qty=r.fill_qty,
        fill_avg_price=float(r.fill_avg_price) if r.fill_avg_price is not None else None,
        realized_pnl=float(r.realized_pnl) if r.realized_pnl is not None else None,
        reviewed_at=r.reviewed_at,
    )


class PostgresDecisionLog:
    def __init__(self) -> None:
        self._session_factory = async_session_factory()

    async def record(self, entry: DecisionEntry) -> DecisionEntry:
        async with self._session_factory() as session:
            row = AgentDecision(
                # DecisionEntry.id is a short opaque string; we ignore it
                # and let Postgres assign a UUID4. The original opaque id
                # was a session-local identifier — production rows are
                # addressed by their UUID PK.
                id=uuid.uuid4(),
                user_id=uuid.UUID(entry.user_id) if entry.user_id else FIXTURE_USER_ID,
                symbol=entry.symbol,
                horizon=entry.horizon,
                regime=entry.regime,
                proposal=entry.raw_state if isinstance(entry.raw_state, dict) else None,
                risk_approved=entry.risk_approved,
                risk_veto_rule=entry.risk_veto_rule,
                final_action=entry.final_action,
                triggered_at=entry.triggered_at,
                # Reflection-loop columns from migration 0003
                selected_strategy=entry.selected_strategy,
                selector_confidence=entry.selector_confidence,
                selector_rationale=entry.selector_rationale,
                technical_score=entry.technical_score,
                fundamental_score=entry.fundamental_score,
                macro_score=entry.macro_score,
                fill_qty=entry.fill_qty,
                fill_avg_price=entry.fill_avg_price,
                realized_pnl=entry.realized_pnl,
                reviewed_at=entry.reviewed_at,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        # Mutate the entry to reflect the assigned id (callers persist
        # decision_id from the runtime result dict).
        entry.id = str(row.id)
        return entry

    async def list_pending_reflection(
        self,
        *,
        since: timedelta = timedelta(hours=24),
        limit: int = 200,
    ) -> list[DecisionEntry]:
        """Pulls rows where ``realized_pnl IS NOT NULL AND reviewed_at IS NULL``
        within the window. Uses the partial index from migration 0003
        (``ix_agent_decisions_pending_reflection``) — the WHERE clause
        below is byte-equal to the index predicate.
        """
        cutoff = _now() - since
        async with self._session_factory() as session:
            stmt = (
                select(AgentDecision)
                .where(
                    AgentDecision.triggered_at >= cutoff,
                    AgentDecision.realized_pnl.is_not(None),
                    AgentDecision.reviewed_at.is_(None),
                )
                .order_by(AgentDecision.triggered_at.asc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_entry(r) for r in rows]

    async def mark_reviewed(self, decision_id: str) -> None:
        try:
            did = uuid.UUID(decision_id)
        except (ValueError, TypeError):
            return
        async with self._session_factory() as session:
            await session.execute(
                update(AgentDecision)
                .where(AgentDecision.id == did, AgentDecision.reviewed_at.is_(None))
                .values(reviewed_at=_now())
            )
            await session.commit()

    async def update_outcome(
        self,
        decision_id: str,
        *,
        fill_qty: int | None = None,
        fill_avg_price: float | None = None,
        realized_pnl: float | None = None,
    ) -> None:
        try:
            did = uuid.UUID(decision_id)
        except (ValueError, TypeError):
            return
        # Only update columns the caller named — None means "leave alone".
        values: dict[str, object] = {}
        if fill_qty is not None:
            values["fill_qty"] = fill_qty
        if fill_avg_price is not None:
            values["fill_avg_price"] = fill_avg_price
        if realized_pnl is not None:
            values["realized_pnl"] = realized_pnl
        if not values:
            return
        async with self._session_factory() as session:
            await session.execute(
                update(AgentDecision).where(AgentDecision.id == did).values(**values)
            )
            await session.commit()

    async def all_decisions(self) -> list[DecisionEntry]:
        """Debug / testing only — full snapshot."""
        async with self._session_factory() as session:
            stmt = select(AgentDecision).order_by(AgentDecision.triggered_at.desc())
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_entry(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# StrategyConfidenceStore
# ─────────────────────────────────────────────────────────────────────


def _conf_to_row(r: StrategyConfidence) -> StrategyConfidenceRow:
    return StrategyConfidenceRow(
        strategy_id=r.strategy_id,
        confidence=float(r.confidence),
        wins=r.wins,
        losses=r.losses,
        last_reflection_at=r.last_reflection_at,
        notes=r.notes,
    )


class PostgresStrategyConfidenceStore:
    """Postgres-backed prior store.

    Migration 0003 seeds five rows at confidence=0.5. We re-seed via
    ``_ensure_seeded`` on first use so a clean DB (test, fresh dev)
    still has the priors the Selector reads.
    """

    def __init__(self) -> None:
        self._session_factory = async_session_factory()
        self._seeded = False

    async def _ensure_seeded(self) -> None:
        if self._seeded:
            return
        async with self._session_factory() as session:
            for sid in STRATEGY_REGISTRY:
                stmt = pg_insert(StrategyConfidence).values(
                    strategy_id=sid, confidence=0.5,
                ).on_conflict_do_nothing(index_elements=["strategy_id"])
                await session.execute(stmt)
            await session.commit()
        self._seeded = True

    async def get(self, strategy_id: str) -> StrategyConfidenceRow:
        await self._ensure_seeded()
        async with self._session_factory() as session:
            row = await session.get(StrategyConfidence, strategy_id)
            if row is None:
                # Unknown id — insert at 0.5 + return.
                row = StrategyConfidence(strategy_id=strategy_id, confidence=0.5)
                session.add(row)
                await session.commit()
                await session.refresh(row)
        return _conf_to_row(row).clamped()

    async def all(self) -> list[StrategyConfidenceRow]:
        await self._ensure_seeded()
        async with self._session_factory() as session:
            rows = (await session.execute(select(StrategyConfidence))).scalars().all()
        return [_conf_to_row(r).clamped() for r in rows]

    async def apply_delta(
        self,
        strategy_id: str,
        *,
        confidence_delta: float,
        wins: int = 0,
        losses: int = 0,
        notes: str = "",
    ) -> StrategyConfidenceRow:
        await self._ensure_seeded()
        # Same double-clamp as the in-memory impl.
        bounded_delta = max(
            -MAX_CONFIDENCE_DELTA_PER_CYCLE,
            min(MAX_CONFIDENCE_DELTA_PER_CYCLE, confidence_delta),
        )

        async with self._session_factory() as session:
            row = await session.get(StrategyConfidence, strategy_id)
            if row is None:
                row = StrategyConfidence(strategy_id=strategy_id, confidence=0.5)
                session.add(row)
                await session.flush()

            new_confidence = max(
                MIN_CONFIDENCE,
                min(MAX_CONFIDENCE, float(row.confidence) + bounded_delta),
            )
            row.confidence = new_confidence  # type: ignore[assignment]
            row.wins = row.wins + wins
            row.losses = row.losses + losses
            row.last_reflection_at = _now()
            if notes:
                row.notes = notes
            await session.commit()
            await session.refresh(row)

        return _conf_to_row(row).clamped()
