"""Ghost P&L evaluator — what non-executed picks would have done.

Selects decisions that never became trades (risk-vetoed, user-declined,
expired) within the lookback window, derives an entry price from the
stored proposal, marks each against daily closes, and finalizes
``ghost_pnl`` once the proposal's horizon has elapsed.

**Options are marked on the CONTRACT, not the underlying.** An option
ghost pulls bars for its OCC symbol via ``get_option_price_provider`` and
scales P&L by the contract multiplier. Both are load-bearing: the stock
bars endpoint returns an empty series (not an error) for an OCC symbol,
so before this every options refusal skipped silently; and omitting the
multiplier under-reports the dollars 100-fold — which is precisely the
number the Refusal Ledger puts on screen.

Deterministic Python over close prices — no LLM in this path. Idempotent
per day: re-running upserts the same marks.

Invoked from ``daily_cron`` (after the council loop) or standalone:

    uv run --package agents python -m trading_agents.jobs.ghost_eval
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select

from engine.db import async_session_factory
from engine.db.models import AgentDecision, GhostOutcome
from engine.prices import get_option_price_provider, get_price_provider

logger = logging.getLogger("agents.ghost_eval")

DEFAULT_HORIZON_DAYS = 5
# Look back horizon + buffer so weekend/holiday gaps still finalize.
LOOKBACK_BUFFER_DAYS = 7

_HORIZON_BY_PROPOSAL_HORIZON = {
    "intraday": 1,
    "short": 5,
    "mid": 10,
    "long": 20,
}


def _is_option(proposal: dict[str, Any]) -> bool:
    """True for an options proposal. Accepts both key styles because the
    DTO writes camelCase (``isOption``) while older/internal rows may
    carry the snake_case form — the same tolerance
    ``position_manager.sweep_expiring_options_for_user`` already applies."""
    return bool(proposal.get("isOption", proposal.get("is_option", False)))


def _multiplier(proposal: dict[str, Any]) -> int:
    """Contract multiplier — 100 for a standard US equity option, 1 for a
    share. Load-bearing for P&L: a $1.00 premium move on 4 contracts is
    $400, not $4."""
    if not _is_option(proposal):
        return 1
    raw = proposal.get("multiplier", proposal.get("multiplier", 100))
    try:
        m = int(raw)
    except (TypeError, ValueError):
        return 100
    return m if m > 0 else 100


def _mark_symbol(row: AgentDecision, proposal: dict[str, Any]) -> str | None:
    """Which symbol the forward marks are pulled for.

    For an option that is the OCC contract, NOT ``row.symbol`` — the
    latter is deliberately the underlying (it is what the one-decision-
    per-symbol-per-day dedup and the whole UI key on), so marking it would
    price the wrong instrument. Returns None when an options row has no
    OCC symbol, so the caller skips instead of silently marking the stock.
    """
    if not _is_option(proposal):
        return row.symbol
    occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
    return str(occ).upper() if occ else None


def _entry_price(proposal: dict[str, Any]) -> tuple[float, str] | None:
    """Entry reference, in the SAME units the forward marks arrive in.

    For an option that unit is the per-share premium (what an option bar's
    close quotes), not the per-contract cost. So the notional fallback
    divides by the multiplier — ``estimatedNotional`` for options is
    ``premium * qty * 100`` and would otherwise read as a $217 premium on
    a $2.17 contract. ``limitPrice`` is already per-share, both sides.
    """
    limit = proposal.get("limitPrice", proposal.get("limit_price"))
    if isinstance(limit, (int, float)) and limit > 0:
        return float(limit), "proposal_limit"
    qty = proposal.get("qty")
    notional = proposal.get("estimatedNotional", proposal.get("estimated_notional"))
    if (
        isinstance(qty, (int, float))
        and qty
        and isinstance(notional, (int, float))
        and notional > 0
    ):
        per_unit = float(notional) / float(qty) / float(_multiplier(proposal))
        return per_unit, "proposal_notional"
    return None


def _skip_reason(
    *,
    reason: str | None,
    entry: tuple[float, str] | None,
    mark_symbol: str | None,
    side: Any,
    qty: Any,
) -> str | None:
    """Which prefilter check failed, or None if the row is evaluable.

    Named so ``evaluate_ghosts`` can report WHICH branch fired instead of
    one bare ``skipped`` total — a high count with no breakdown isn't
    diagnosable (see IMPL_REFUSAL_LEDGER.md §1).
    """
    if reason is None:
        return "reason_is_none"
    if entry is None:
        return "entry_is_none"
    if mark_symbol is None:
        return "mark_symbol_is_none"
    if side not in ("BUY", "SELL"):
        return "bad_side"
    if not qty:
        return "falsy_qty"
    return None


def _reason_of(row: AgentDecision) -> str | None:
    if not row.risk_approved:
        return "vetoed"
    if row.user_response in ("declined", "rejected"):
        return "declined"
    if row.user_response == "expired":
        return "expired"
    return None


def _ghost_pnl(side: str, qty: int, entry: float, mark: float, multiplier: int = 1) -> float:
    """Dollar P&L of the refused trade.

    ``multiplier`` is 1 for equities and 100 for a standard option
    contract. Omitting it made every options ghost 100x too small — the
    exact number the Refusal Ledger reports, so it is not cosmetic.
    """
    direction = 1.0 if side == "BUY" else -1.0
    return round(direction * qty * (mark - entry) * multiplier, 2)


def _trading_day_offset(start: date, day: date) -> int:
    """Count trading days (Mon-Fri) strictly after ``start`` up to ``day``."""
    if day <= start:
        return 0
    offset = 0
    d = start
    while d < day:
        d += timedelta(days=1)
        if d.weekday() < 5:
            offset += 1
    return offset


async def evaluate_ghosts(*, today: date | None = None) -> dict[str, int | dict[str, int]]:
    """One evaluator pass. Returns counters for logging/tests: ``created``/
    ``updated``/``finalized``/``skipped`` (ints) plus ``skip_reasons``, a
    breakdown of ``skipped`` by which check failed (``reason_is_none``,
    ``entry_is_none``, ``mark_symbol_is_none``, ``bad_side``,
    ``falsy_qty``, ``no_daily_closes``, ``marks_out_of_window``) — a bare
    total doesn't say why."""
    today = today or datetime.now(UTC).date()
    session_factory = async_session_factory()
    created = updated = finalized = skipped = 0
    skip_reasons: dict[str, int] = {}

    def _skip(reason_name: str) -> None:
        nonlocal skipped
        skipped += 1
        skip_reasons[reason_name] = skip_reasons.get(reason_name, 0) + 1

    async with session_factory() as session:
        cutoff = datetime.now(UTC) - timedelta(
            days=max(_HORIZON_BY_PROPOSAL_HORIZON.values()) + LOOKBACK_BUFFER_DAYS
        )
        stmt = (
            select(AgentDecision)
            .where(
                AgentDecision.triggered_at >= cutoff,
                AgentDecision.proposal.is_not(None),
                or_(
                    AgentDecision.risk_approved.is_(False),
                    AgentDecision.user_response.in_(["declined", "rejected", "expired"]),
                ),
            )
            .order_by(AgentDecision.triggered_at.asc())
        )
        decisions = (await session.execute(stmt)).scalars().all()

        for row in decisions:
            reason = _reason_of(row)
            proposal = row.proposal or {}
            entry = _entry_price(proposal)
            side = proposal.get("side")
            qty = proposal.get("qty")
            mark_symbol = _mark_symbol(row, proposal)
            reason_to_skip = _skip_reason(
                reason=reason, entry=entry, mark_symbol=mark_symbol, side=side, qty=qty
            )
            if reason_to_skip is not None:
                _skip(reason_to_skip)
                continue
            # `_skip_reason` already guarantees these — restate for mypy,
            # which can't narrow through the helper call the way it could
            # through an inline `is None` check.
            assert entry is not None
            assert mark_symbol is not None
            assert qty
            entry_price, entry_source = entry
            multiplier = _multiplier(proposal)
            is_option = _is_option(proposal)
            horizon = _HORIZON_BY_PROPOSAL_HORIZON.get(row.horizon, DEFAULT_HORIZON_DAYS)
            start_day = row.triggered_at.date()

            ghost = (
                await session.execute(
                    select(GhostOutcome).where(GhostOutcome.decision_id == row.id)
                )
            ).scalar_one_or_none()
            if ghost is None:
                ghost = GhostOutcome(
                    id=uuid.uuid4(),
                    decision_id=row.id,
                    reason=reason,
                    side=str(side),
                    qty=int(qty),
                    entry_price=Decimal(str(round(entry_price, 4))),
                    entry_source=entry_source,
                    horizon_days=horizon,
                    marks={},
                    status="pending",
                    first_evaluated_at=datetime.now(UTC),
                )
                session.add(ghost)
                created += 1
            elif ghost.status == "final":
                continue  # idempotent: nothing to do

            # An option is marked on its OWN contract's bars. Marking the
            # underlying's stock bars would answer a different question
            # (and, before this branch existed, returned [] for every OCC
            # symbol — so options never appeared in the ledger at all).
            provider = (
                get_option_price_provider(anchor_price=entry_price, anchor_day=start_day)
                if is_option
                else get_price_provider(anchor_price=entry_price, anchor_day=start_day)
            )
            closes = await provider.daily_closes(mark_symbol, start_day, today)
            if not closes:
                _skip("no_daily_closes")
                continue

            marks: dict[str, float] = dict(ghost.marks or {})
            for c in closes:
                off = _trading_day_offset(start_day, c.day)
                if 1 <= off <= horizon:
                    marks[str(off)] = c.close

            if not marks:
                _skip("marks_out_of_window")
                continue

            last_offset = max(int(k) for k in marks)
            last_price = marks[str(last_offset)]
            ghost.marks = marks
            ghost.last_price = Decimal(str(round(last_price, 4)))
            ghost.ghost_pnl = Decimal(
                str(_ghost_pnl(str(side), int(qty), entry_price, last_price, multiplier))
            )
            ghost.price_source = provider.name
            ghost.last_evaluated_at = datetime.now(UTC)
            # Finalize on ELAPSED trading days, not on the last day that
            # happened to print a bar. Keying on the latter left a ghost
            # "partial" forever whenever the horizon's final session had no
            # bar — rare for a liquid stock, routine for an option, where a
            # thin strike may print on 3 days out of 10 (verified live
            # 2026-08-30). A ledger that never finalizes reports nothing.
            elapsed = _trading_day_offset(start_day, today)
            new_status = "final" if elapsed >= horizon else "partial"
            if new_status == "final":
                finalized += 1
            ghost.status = new_status
            updated += 1

        await session.commit()

    counters: dict[str, int | dict[str, int]] = {
        "created": created,
        "updated": updated,
        "finalized": finalized,
        "skipped": skipped,
        "skip_reasons": skip_reasons,
    }
    logger.info("ghost_eval pass: %s", counters)
    return counters


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(evaluate_ghosts())
