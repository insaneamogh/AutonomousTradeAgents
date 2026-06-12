"""In-memory store for Phase 0.

Replaced in Phase 1 by SQLAlchemy queries hitting Postgres. Same shapes,
same interfaces — only the storage backend changes. Keep this thin so the
swap is painless.

Thread-safety: an ``asyncio.Lock`` guards the mutable state. Single-process
deployments are fine; Phase 1's real DB makes multi-replica safe.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.schemas.account import AccountResponse
from app.schemas.activity import ActivityEntryDto
from app.schemas.approvals import (
    ApprovalProposalDto,
    DecisionOutcome,
    DecisionResponse,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _minutes_ago(n: int) -> datetime:
    return _now() - timedelta(minutes=n)


class MockStore:
    """Single-process, lock-guarded in-memory store. Lives for the process lifetime."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._account = self._seed_account()
        self._pending: list[ApprovalProposalDto] = self._seed_pending()
        self._activity: list[ActivityEntryDto] = self._seed_activity()
        self._decisions: dict[str, DecisionResponse] = {}

    # ── Account ──────────────────────────────────────────────────────

    async def get_account(self) -> AccountResponse:
        async with self._lock:
            return self._account

    # ── Activity ─────────────────────────────────────────────────────

    async def list_activity(self, limit: int = 50) -> list[ActivityEntryDto]:
        async with self._lock:
            return list(self._activity[:limit])

    async def append_activity(self, entry: ActivityEntryDto) -> None:
        async with self._lock:
            self._activity.insert(0, entry)

    # ── Approvals ────────────────────────────────────────────────────

    async def append_pending(self, proposal: ApprovalProposalDto) -> ApprovalProposalDto:
        async with self._lock:
            # Idempotent on id — re-submitting the same proposal returns the existing one.
            existing = next((p for p in self._pending if p.id == proposal.id), None)
            if existing is not None:
                return existing
            self._pending.append(proposal)
            self._activity.insert(
                0,
                ActivityEntryDto(
                    id=f"act-{proposal.id}-proposed",
                    kind="proposal",
                    symbol=proposal.symbol,
                    side=proposal.side,
                    qty=proposal.qty,
                    headline=(
                        f"Agent proposed {proposal.side} {proposal.qty} {proposal.symbol} "
                        f"— confidence {proposal.conviction_level}/5."
                    ),
                    timestamp=proposal.proposed_at,
                ),
            )
            return proposal

    async def list_pending(self) -> list[ApprovalProposalDto]:
        async with self._lock:
            now = _now()
            # Auto-expire stale proposals.
            self._pending = [p for p in self._pending if (p.expires_at is None or p.expires_at > now)]
            return list(self._pending)

    async def decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        exit_mode: str | None = None,
    ) -> DecisionResponse | None:
        # ``exit_mode`` is persisted on the Postgres path (agent_decisions
        # column). The in-memory store has no decision row to carry it —
        # accepted here for protocol parity.
        _ = exit_mode
        async with self._lock:
            match = next((p for p in self._pending if p.id == proposal_id), None)
            if match is None:
                return None
            self._pending = [p for p in self._pending if p.id != proposal_id]
            decision = DecisionResponse(
                proposal_id=proposal_id,
                outcome=outcome,
                decided_at=_now(),
            )
            self._decisions[proposal_id] = decision

            # Mirror the decision into the activity feed so the Home screen reflects it.
            self._activity.insert(
                0,
                ActivityEntryDto(
                    id=f"act-{proposal_id}-{outcome}",
                    kind="approved" if outcome == "approved" else "declined",
                    symbol=match.symbol,
                    side=match.side,
                    qty=match.qty,
                    headline=(
                        f"You {outcome} the agent's "
                        f"{match.side} {match.qty} {match.symbol} proposal."
                    ),
                    timestamp=decision.decided_at,
                ),
            )
            return decision

    # ── Seeders ──────────────────────────────────────────────────────

    @staticmethod
    def _seed_account() -> AccountResponse:
        return AccountResponse(
            equity=102_847.31,
            cash=38_412.55,
            buying_power=76_825.10,
            today_pnl=412.85,
            today_pnl_pct=0.40,
            open_positions=4,
            status="connected",
            broker_name="Alpaca",
            is_paper=True,
        )

    @staticmethod
    def _seed_pending() -> list[ApprovalProposalDto]:
        now = _now()
        return [
            ApprovalProposalDto(
                id="pending-1",
                symbol="NVDA",
                side="BUY",
                qty=12,
                order_type="MARKET",
                limit_price=None,
                estimated_notional=11_258.40,
                rationale=(
                    "Council 3-of-4 specialists positive. Trend filter intact above 50-DMA; "
                    "relative strength vs. SMH firm. Risk sized at 3.2% of equity with a 4% stop."
                ),
                bull_case=(
                    "Earnings revisions remain positive. AI-capex narrative supports continued "
                    "multiple. Relative strength vs. peers is in the top decile. Volume profile "
                    "constructive — accumulation pattern over the last 8 sessions."
                ),
                bear_case=(
                    "Sentiment is crowded; insider sells last week add a counter-signal. If "
                    "AI-capex narrative softens, the multiple compresses fast. Strongest "
                    "contrary signal: short interest moving up despite the rally."
                ),
                risk_level=2,
                conviction_level=4,
                proposed_at=now - timedelta(minutes=2),
                expires_at=now + timedelta(minutes=14, seconds=23),
            ),
        ]

    @staticmethod
    def _seed_activity() -> list[ActivityEntryDto]:
        return [
            ActivityEntryDto(
                id="act-1",
                kind="filled",
                symbol="AAPL",
                side="BUY",
                qty=21,
                price=187.40,
                headline="Filled @ $187.40 — followed playbook, 3.2% size",
                timestamp=_minutes_ago(7),
            ),
            ActivityEntryDto(
                id="act-2",
                kind="vetoed",
                symbol="HMT",
                side="BUY",
                verdict="HOLD",
                headline="Vetoed — bear researcher flagged 200-DMA divergence",
                timestamp=_minutes_ago(54),
            ),
            ActivityEntryDto(
                id="act-3",
                kind="approved",
                symbol="MSFT",
                side="SELL",
                qty=8,
                price=432.18,
                headline="Closed +1.8R — trailing stop hit on weekly weakness",
                timestamp=_minutes_ago(122),
            ),
            ActivityEntryDto(
                id="act-4",
                kind="declined",
                symbol="TSLA",
                side="BUY",
                headline="User declined — preferred to wait for earnings",
                timestamp=_minutes_ago(180),
            ),
        ]


# NOTE: ``get_store`` lives in ``app.services.store`` now — it dispatches
# between MockStore and PostgresStore based on the USE_POSTGRES env. Routers
# import the factory from there, not from this module. Direct imports of
# MockStore are reserved for tests that want to bypass the factory.
