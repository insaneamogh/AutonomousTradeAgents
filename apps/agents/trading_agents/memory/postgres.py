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
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from engine.db import async_session_factory
from engine.db.models import AgentDecision, StrategyConfidence
from trading_agents.memory.decision_log import ALL_USERS, DecisionEntry
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
    return datetime.now(UTC)


class _NoSuchTenant(Exception):
    """``user_id`` wasn't a UUID, so it can't match any row."""


def _tenant_uuid(user_id: str) -> uuid.UUID | None:
    """UUID to filter ``AgentDecision.user_id`` on, or None for ALL_USERS.

    A malformed id raises rather than silently widening the query — an
    unparseable tenant must never degrade into "return everything".
    """
    if user_id == ALL_USERS:
        return None
    try:
        return uuid.UUID(user_id)
    except (ValueError, TypeError) as exc:
        raise _NoSuchTenant(user_id) from exc


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
        degraded_nodes=list(r.degraded_nodes) if r.degraded_nodes else None,
        technical=r.technical,
        fundamental=r.fundamental,
        macro=r.macro,
        analyst_subset=list(r.analyst_subset) if r.analyst_subset else None,
        bull_case=r.bull_case,
        bear_case=r.bear_case,
        risk_reason=r.risk_reason,
        token_usage=r.token_usage,
        completed_at=r.completed_at,
    )


class PostgresDecisionLog:
    def __init__(self) -> None:
        self._session_factory = async_session_factory()

    async def record(self, entry: DecisionEntry) -> DecisionEntry:
        # entry.id is council_run_id when runtime.run_council built this entry
        # (a real UUID string generated before any LLM call in the pass) —
        # reusing it as the row's PK ties this decision to every llm_calls
        # row the cost ledger correlated under the same id (see
        # CostLedger.backfill_decision_id), with no extra lookup needed. Any
        # caller that doesn't go through that path — or still uses the legacy
        # opaque ``"dec-..."`` id — falls back to a fresh uuid4(), exactly as
        # this always did.
        try:
            row_id = uuid.UUID(entry.id)
        except (ValueError, TypeError):
            row_id = uuid.uuid4()
        async with self._session_factory() as session:
            row = AgentDecision(
                id=row_id,
                user_id=uuid.UUID(entry.user_id) if entry.user_id else FIXTURE_USER_ID,
                symbol=entry.symbol,
                horizon=entry.horizon,
                regime=entry.regime,
                # When the risk officer approved, ``proposal`` holds the
                # camelCase DTO so the API's list_pending() can parse this
                # row directly (single write path). Otherwise, when a
                # proposal was drafted but vetoed, we keep the Drafter's
                # own (snake_case) dict — rationale/bull_case/bear_case/
                # qty/side survive for the audit trail even though nothing
                # executed. A genuine HOLD (nothing ever drafted) writes
                # None here, not the raw_state ENVELOPE ``{regime,
                # proposal, analyst_subset, degraded_nodes}`` this used to
                # fall back to — that dict is truthy even when its own
                # "proposal" key is null, so every HOLD's biography read
                # ``proposal.get("side")`` as None and ``.get("rationale")``
                # as "", rendering as a bare "Council proposed HOLD X"
                # with an empty detail no matter what the council actually
                # found. ``regime``/``analyst_subset``/``degraded_nodes``
                # are already their own dedicated columns below, so the
                # envelope carried nothing the row didn't already have.
                proposal=entry.proposal_dto
                or (
                    entry.raw_state.get("proposal")
                    if isinstance(entry.raw_state, dict)
                    else None
                ),
                risk_approved=entry.risk_approved,
                risk_veto_rule=entry.risk_veto_rule,
                risk_reason=entry.risk_reason,
                final_action=entry.final_action,
                triggered_at=entry.triggered_at,
                # Full council audit surface (WP0)
                analyst_subset=entry.analyst_subset,
                technical=entry.technical,
                fundamental=entry.fundamental,
                macro=entry.macro,
                bull_case=entry.bull_case,
                bear_case=entry.bear_case,
                token_usage=entry.token_usage,
                completed_at=entry.completed_at,
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
                degraded_nodes=entry.degraded_nodes,
                reasoning=entry.reasoning,
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
        user_id: str,
        since: timedelta = timedelta(hours=24),
        limit: int = 200,
    ) -> list[DecisionEntry]:
        """Pulls rows where ``realized_pnl IS NOT NULL AND reviewed_at IS NULL``
        AND ``closed_at`` falls within ``since``, scoped to ``user_id`` (or
        every tenant when the caller passes ``ALL_USERS``).

        Anchored on ``closed_at`` (migration 0009 — when the position from
        this decision actually went flat), NOT ``triggered_at`` (when the
        decision/entry order was placed). Those two can be days apart for
        an ordinary swing position, and ``triggered_at`` only ever gets
        OLDER relative to "now" — so anchoring the window on it made every
        decision that took longer than ``since`` to close permanently
        invisible to Reflection the moment it finally did close, not just
        late for one cycle. Confirmed live 2026-09-01: 6/6 real closed
        decisions (triggered ~117h earlier, closed ~48h earlier — both past
        the daily cron's 24h ``since``) still had ``reviewed_at IS NULL``,
        and every ``strategy_confidence`` row was still sitting at the
        migration-0003 seed value (confidence=0.500, wins=0, losses=0,
        last_reflection_at=None) — Reflection had never once fired against
        real production data, which is why the Review screen's agreement
        stat reads a flat 0%: every prior sits dead-center in the "neutral"
        band, and neutral can never register as agreement.

        Uses the index from migration 0017
        (``ix_agent_decisions_pending_reflection``, now on ``closed_at``)
        — the trailing WHERE clauses are byte-equal to the index predicate.
        """
        try:
            tenant = _tenant_uuid(user_id)
        except _NoSuchTenant:
            return []
        cutoff = _now() - since
        conditions = [
            AgentDecision.closed_at.is_not(None),
            AgentDecision.closed_at >= cutoff,
            AgentDecision.realized_pnl.is_not(None),
            AgentDecision.reviewed_at.is_(None),
        ]
        if tenant is not None:
            conditions.append(AgentDecision.user_id == tenant)
        async with self._session_factory() as session:
            stmt = (
                select(AgentDecision)
                .where(*conditions)
                .order_by(AgentDecision.closed_at.asc())
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

    async def all_decisions(self, *, user_id: str) -> list[DecisionEntry]:
        """Every decision for ``user_id`` — or the whole book under ALL_USERS.

        Filters on the indexed ``agent_decisions.user_id`` column so the
        API's aggregate reads can never see another tenant's rows.
        """
        try:
            tenant = _tenant_uuid(user_id)
        except _NoSuchTenant:
            return []
        conditions = [] if tenant is None else [AgentDecision.user_id == tenant]
        async with self._session_factory() as session:
            stmt = (
                select(AgentDecision)
                .where(*conditions)
                .order_by(AgentDecision.triggered_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_entry(r) for r in rows]

    async def has_decision_today(
        self, *, user_id: str | None, symbol: str, day_utc: str
    ) -> bool:
        """Indexed (user_id, symbol, triggered_at) existence check — replaces
        the daily cron's full-history scan. ``day_utc`` is YYYY-MM-DD."""
        from datetime import date as _date

        uid = uuid.UUID(user_id) if user_id else FIXTURE_USER_ID
        y, m, d = (int(p) for p in day_utc.split("-"))
        day_start = datetime(y, m, d, tzinfo=UTC)
        day_end = datetime.combine(_date(y, m, d), datetime.max.time(), tzinfo=UTC)
        async with self._session_factory() as session:
            stmt = (
                select(AgentDecision.id)
                .where(
                    AgentDecision.user_id == uid,
                    AgentDecision.symbol == symbol,
                    AgentDecision.triggered_at >= day_start,
                    AgentDecision.triggered_at <= day_end,
                )
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def minutes_since_last_decision(
        self, *, user_id: str | None, symbol: str
    ) -> float | None:
        """Minutes since the newest decision for (user, symbol). Same
        (user_id, symbol, triggered_at) index the daily check uses."""
        uid = uuid.UUID(user_id) if user_id else FIXTURE_USER_ID
        async with self._session_factory() as session:
            stmt = (
                select(AgentDecision.triggered_at)
                .where(
                    AgentDecision.user_id == uid,
                    AgentDecision.symbol == symbol,
                )
                .order_by(AgentDecision.triggered_at.desc())
                .limit(1)
            )
            newest = (await session.execute(stmt)).scalar_one_or_none()
        if newest is None:
            return None
        return (datetime.now(UTC) - newest).total_seconds() / 60.0


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
