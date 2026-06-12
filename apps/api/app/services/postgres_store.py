"""PostgresStore — SQLAlchemy-backed Store implementation.

Reads/writes the ``engine.db`` schema. Phase 0 maps the ApprovalProposalDto
to/from the ``agent_decisions`` table:

  - proposal-side fields go into denormalized columns where indexed lookups
    matter (symbol, side via proposal JSONB, risk_approved, user_response)
  - the full DTO is stored as the ``proposal`` JSONB column for round-trip

Account snapshot: Phase 0 returns a fixture, same as MockStore. Phase 1's
reconciler will cache the real Alpaca response in a positions/snapshots
table and this method will read from there.

User: Phase 0 ensures a single default user exists at init time. Phase 3
plugs in real auth and the per-user row.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import desc, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.account import AccountResponse
from app.schemas.activity import ActivityEntryDto
from app.schemas.approvals import (
    ApprovalProposalDto,
    DecisionOutcome,
    DecisionResponse,
)
from engine.db import async_session_factory
from engine.db.models import AgentDecision, User

logger = logging.getLogger("api.store.postgres")


# A fixed UUID for the Phase 0 single-user fixture. Phase 3 replaces this
# with auth-derived user ids. Hardcoded so re-runs are idempotent.
DEFAULT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_USER_EMAIL = "demo@local.dev"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PostgresStore:
    """SQLAlchemy 2.0 async store. Idempotent ``ensure_seed()`` on first use."""

    def __init__(self) -> None:
        self._session_factory = async_session_factory()
        self._seeded = False

    async def _ensure_seed(self, session: AsyncSession) -> None:
        if self._seeded:
            return
        # Upsert default user. ON CONFLICT DO NOTHING keeps it idempotent across boots.
        stmt = pg_insert(User).values(
            id=DEFAULT_USER_ID,
            email=DEFAULT_USER_EMAIL,
            display_name="Demo (Phase 0)",
        ).on_conflict_do_nothing(index_elements=["id"])
        await session.execute(stmt)
        await session.commit()
        self._seeded = True
        logger.info("PostgresStore: default user ensured (id=%s)", DEFAULT_USER_ID)

    # ── Account ──────────────────────────────────────────────────────

    async def get_account(self) -> AccountResponse:
        """Most-recent reconciler snapshot for the default user, or a
        cold-boot fixture if the reconciler hasn't run yet."""
        from engine.db.models import PositionsSnapshot

        async with self._session_factory() as session:
            await self._ensure_seed(session)
            stmt = (
                select(PositionsSnapshot)
                .where(PositionsSnapshot.user_id == DEFAULT_USER_ID)
                .order_by(desc(PositionsSnapshot.captured_at))
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()

        if row is None:
            # Cold boot — reconciler hasn't landed a snapshot yet.
            return AccountResponse(
                equity=100_000.00,
                cash=100_000.00,
                buying_power=200_000.00,
                today_pnl=0.0,
                today_pnl_pct=0.0,
                open_positions=0,
                status="connected",
                broker_name="Alpaca",
                is_paper=True,
            )

        return AccountResponse(
            equity=float(row.account_equity),
            cash=float(row.cash),
            buying_power=float(row.buying_power),
            today_pnl=float(row.daily_pnl or 0),
            today_pnl_pct=float(row.daily_pnl_pct or 0),
            open_positions=len(row.open_positions or []),
            status="connected",
            broker_name=row.source if row.source != "mock" else "Alpaca",
            is_paper=True,
        )

    # ── Activity ─────────────────────────────────────────────────────

    async def list_activity(self, limit: int = 50) -> list[ActivityEntryDto]:
        """Derive activity from agent_decisions, newest first.

        Mapping per decision:
            user_response='approved'  → kind='approved'
            user_response='declined'  → kind='declined'
            user_response='expired'   → kind='declined' (treated as user-skip)
            risk_approved=False       → kind='vetoed'
            otherwise                 → kind='proposal'
        """
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            stmt = (
                select(AgentDecision)
                .where(AgentDecision.user_id == DEFAULT_USER_ID)
                .order_by(desc(AgentDecision.triggered_at))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()

        return [_decision_to_activity(row) for row in rows]

    async def append_activity(self, entry: ActivityEntryDto) -> None:
        # Activity in PostgresStore is derived from agent_decisions — there's
        # no separate write path. This method exists only to satisfy the
        # Protocol so MockStore/PostgresStore are drop-in interchangeable.
        logger.debug("PostgresStore.append_activity is a no-op (derived view)")

    # ── Approvals / pending ──────────────────────────────────────────

    async def list_pending(self) -> list[ApprovalProposalDto]:
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            stmt = (
                select(AgentDecision)
                .where(
                    AgentDecision.user_id == DEFAULT_USER_ID,
                    AgentDecision.risk_approved.is_(True),
                    AgentDecision.user_response.is_(None),
                )
                .order_by(desc(AgentDecision.triggered_at))
            )
            rows = (await session.execute(stmt)).scalars().all()

        out: list[ApprovalProposalDto] = []
        now = _now()
        for row in rows:
            dto = _row_to_proposal_dto(row)
            if dto is None:
                continue
            if dto.expires_at is not None and dto.expires_at < now:
                # Stale — auto-expire (we don't write back here to keep this method idempotent).
                continue
            out.append(dto)
        return out

    async def append_pending(self, proposal: ApprovalProposalDto) -> ApprovalProposalDto:
        proposal_json = _dto_to_json(proposal)
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            stmt = pg_insert(AgentDecision).values(
                id=uuid.UUID(int=int(proposal.id.replace("-", "")[:32], 16))
                if proposal.id.replace("-", "").isalnum() and len(proposal.id.replace("-", "")) >= 32
                else uuid.uuid4(),
                user_id=DEFAULT_USER_ID,
                symbol=proposal.symbol,
                horizon="short",
                proposal=proposal_json,
                bull_case=proposal.bull_case,
                bear_case=proposal.bear_case,
                risk_approved=True,
                risk_reason="All risk checks passed.",
                approval_mode="ask",
                final_action=proposal.side,
                triggered_at=proposal.proposed_at,
            ).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)
            await session.commit()
        return proposal

    async def decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        exit_mode: str | None = None,
    ) -> DecisionResponse | None:
        now = _now()
        async with self._session_factory() as session:
            await self._ensure_seed(session)
            # Resolve the row by looking at the proposal JSONB's `id` field.
            stmt = (
                select(AgentDecision)
                .where(
                    AgentDecision.user_id == DEFAULT_USER_ID,
                    AgentDecision.proposal["id"].astext == proposal_id,
                    AgentDecision.user_response.is_(None),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None

            values: dict[str, Any] = {
                "user_response": outcome,
                "user_responded_at": now,
                "completed_at": now,
            }
            if exit_mode in ("agent", "manual"):
                # The user's per-position close delegation, chosen on the
                # approval card. The position manager only touches
                # exit_mode='agent' rows.
                values["exit_mode"] = exit_mode

            await session.execute(
                update(AgentDecision).where(AgentDecision.id == row.id).values(**values)
            )
            await session.commit()

        return DecisionResponse(proposal_id=proposal_id, outcome=outcome, decided_at=now)


# ─────────────────────────────────────────────────────────────────────
# Mappers
# ─────────────────────────────────────────────────────────────────────


def _dto_to_json(dto: ApprovalProposalDto) -> dict[str, Any]:
    """Pydantic v2 model_dump with `mode=json` → ISO-formatted datetimes."""
    return dto.model_dump(mode="json", by_alias=True)


def _row_to_proposal_dto(row: AgentDecision) -> ApprovalProposalDto | None:
    if not row.proposal:
        return None
    try:
        return ApprovalProposalDto.model_validate(row.proposal)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse proposal JSONB for row %s: %s", row.id, exc)
        return None


def _decision_to_activity(row: AgentDecision) -> ActivityEntryDto:
    # Derive kind from row state.
    side_val = cast(str, (row.proposal or {}).get("side", "BUY"))
    qty_val = (row.proposal or {}).get("qty")

    if row.user_response == "approved":
        kind = "approved"
        headline = f"You approved the agent's {side_val} {qty_val or ''} {row.symbol} proposal."
    elif row.user_response in ("declined", "expired"):
        kind = "declined"
        headline = f"You {row.user_response} the {side_val} {row.symbol} proposal."
    elif not row.risk_approved:
        kind = "vetoed"
        headline = f"Vetoed — {row.risk_veto_rule or 'risk rule fired'}."
    else:
        kind = "proposal"
        headline = f"Agent proposed {side_val} {qty_val or ''} {row.symbol}."

    return ActivityEntryDto.model_validate({
        "id": f"act-{row.id}",
        "kind": kind,
        "symbol": row.symbol,
        "side": side_val,
        "qty": int(qty_val) if qty_val is not None else None,
        "price": None,
        "verdict": None,
        "headline": headline.strip(),
        "timestamp": row.user_responded_at or row.completed_at or row.triggered_at,
    })
