"""Ghost P&L + veto-ledger aggregates.

Reads ``ghost_outcomes`` (joined to ``agent_decisions``) and reduces to
the two headline numbers — "the risk engine saved you $X" and "your
passes cost you $Y" — plus the per-rule veto scorecard.

Both builders take a REQUIRED ``user_id`` and filter on the indexed
``agent_decisions.user_id``. They previously aggregated the whole table,
so /ghost/summary and /risk/vetoes reported other tenants' blocked
notional and ghost P&L as the caller's own numbers.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from engine.db import async_session_factory
from engine.db.models import AgentDecision, GhostOutcome
from trading_agents.memory.decision_log import ALL_USERS


class _NoSuchTenant(Exception):
    """``user_id`` wasn't a UUID, so it can't match any decision row."""


def _tenant_filters(user_id: str) -> list[ColumnElement[bool]]:
    """WHERE fragment scoping a query to ``user_id``.

    Empty only for the ``ALL_USERS`` sentinel. A malformed id raises —
    an unparseable tenant must never widen into "every row".
    """
    if user_id == ALL_USERS:
        return []
    try:
        return [AgentDecision.user_id == uuid.UUID(user_id)]
    except (ValueError, TypeError) as exc:
        raise _NoSuchTenant(user_id) from exc


@dataclass
class GhostBucket:
    count: int
    ghost_pnl: float
    pending_count: int


@dataclass
class GhostSummary:
    window_days: int
    as_of: datetime
    vetoed: GhostBucket
    declined: GhostBucket
    saved_usd: float
    missed_usd: float


@dataclass
class VetoRuleRow:
    rule: str
    count: int
    blocked_notional: float
    ghost_pnl: float | None
    prevented_loss_usd: float | None
    last_at: datetime | None


@dataclass
class TrimRuleRow:
    """One rule that SHRANK trades rather than blocking them."""

    rule: str
    count: int
    """How many approved trades this rule resized."""


@dataclass
class VetoLedger:
    window_days: int
    total_vetoes: int
    total_blocked_notional: float
    rules: list[VetoRuleRow]
    trims: list[TrimRuleRow] = field(default_factory=list)
    """Partial refusals, kept in their own list rather than mixed into
    ``rules``. A trim approved a smaller trade; a veto approved nothing.
    Summing them together would inflate the veto count with events that
    did not stop a trade — the same class of error that once let every
    strategy-fit HOLD land in this ledger as ``unnamed_rule``."""

    total_trims: int = 0


def _empty_summary(window_days: int) -> GhostSummary:
    """Zeroed summary — what an unknown tenant sees."""
    empty = GhostBucket(count=0, ghost_pnl=0.0, pending_count=0)
    return GhostSummary(
        window_days=window_days,
        as_of=datetime.now(UTC),
        vetoed=empty,
        declined=empty,
        saved_usd=0.0,
        missed_usd=0.0,
    )


async def build_ghost_summary(window_days: int = 30, *, user_id: str) -> GhostSummary:
    """Vetoed/declined ghost P&L for ``user_id`` over the window."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    try:
        tenant = _tenant_filters(user_id)
    except _NoSuchTenant:
        return _empty_summary(window_days)
    session_factory = async_session_factory()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(GhostOutcome, AgentDecision.triggered_at)
                    .join(AgentDecision, AgentDecision.id == GhostOutcome.decision_id)
                    .where(AgentDecision.triggered_at >= cutoff, *tenant)
                )
            )
            .all()
        )

    def bucket(reasons: tuple[str, ...]) -> GhostBucket:
        subset = [g for g, _ in rows if g.reason in reasons]
        finals = [g for g in subset if g.status == "final" and g.ghost_pnl is not None]
        return GhostBucket(
            count=len(subset),
            ghost_pnl=round(sum(float(g.ghost_pnl) for g in finals), 2),
            pending_count=len(subset) - len(finals),
        )

    vetoed = bucket(("vetoed",))
    declined = bucket(("declined", "expired"))
    return GhostSummary(
        window_days=window_days,
        as_of=datetime.now(UTC),
        vetoed=vetoed,
        declined=declined,
        # Vetoed picks that WOULD have lost money = savings.
        saved_usd=round(max(0.0, -vetoed.ghost_pnl), 2),
        # Declined picks that WOULD have made money = missed upside.
        missed_usd=round(max(0.0, declined.ghost_pnl), 2),
    )


async def build_veto_ledger(window_days: int = 30, *, user_id: str) -> VetoLedger:
    """Per-rule veto scorecard for ``user_id`` over the window."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    try:
        tenant = _tenant_filters(user_id)
    except _NoSuchTenant:
        return VetoLedger(
            window_days=window_days,
            total_vetoes=0,
            total_blocked_notional=0.0,
            rules=[],
        )
    session_factory = async_session_factory()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AgentDecision, GhostOutcome)
                    .outerjoin(GhostOutcome, GhostOutcome.decision_id == AgentDecision.id)
                    .where(
                        AgentDecision.risk_approved.is_(False),
                        # A veto is a NAMED deterministic rule refusing a
                        # drafted proposal. Filtering on risk_approved
                        # alone also swept up every strategy-fit HOLD —
                        # symbols that matched no setup and so never
                        # reached the risk engine at all. Those have no
                        # rule and no proposal, and they were landing in
                        # the ledger as "unnamed_rule", which both
                        # overstated the veto count and made the per-rule
                        # scorecard read as broken.
                        AgentDecision.risk_veto_rule.is_not(None),
                        AgentDecision.triggered_at >= cutoff,
                        *tenant,
                    )
                )
            )
            .all()
        )

    by_rule: dict[str, list[tuple[AgentDecision, GhostOutcome | None]]] = {}
    for dec, ghost in rows:
        # The query guarantees a rule name; the fallback only guards a
        # row written before rule names were mandatory.
        rule = dec.risk_veto_rule or "unnamed_rule"
        by_rule.setdefault(rule, []).append((dec, ghost))

    out: list[VetoRuleRow] = []
    total_notional = 0.0
    for rule, pairs in by_rule.items():
        notional = 0.0
        ghost_finals: list[float] = []
        last_at: datetime | None = None
        for dec, ghost in pairs:
            p = dec.proposal or {}
            n = p.get("estimatedNotional", p.get("estimated_notional"))
            if isinstance(n, (int, float)):
                notional += float(n)
            if ghost is not None and ghost.status == "final" and ghost.ghost_pnl is not None:
                ghost_finals.append(float(ghost.ghost_pnl))
            if last_at is None or (dec.triggered_at and dec.triggered_at > last_at):
                last_at = dec.triggered_at
        ghost_pnl = round(sum(ghost_finals), 2) if ghost_finals else None
        out.append(
            VetoRuleRow(
                rule=rule,
                count=len(pairs),
                blocked_notional=round(notional, 2),
                ghost_pnl=ghost_pnl,
                prevented_loss_usd=round(max(0.0, -ghost_pnl), 2) if ghost_pnl is not None else None,
                last_at=last_at,
            )
        )
        total_notional += notional

    out.sort(key=lambda r: r.count, reverse=True)
    trims = await _trim_rows(window_days=window_days, tenant=tenant)
    return VetoLedger(
        window_days=window_days,
        total_vetoes=sum(r.count for r in out),
        total_blocked_notional=round(total_notional, 2),
        rules=out,
        trims=trims,
        total_trims=sum(t.count for t in trims),
    )


async def _trim_rows(
    *, window_days: int, tenant: list[ColumnElement[bool]]
) -> list[TrimRuleRow]:
    """Rules that shrank a trade instead of blocking it.

    Read from ``reasoning.risk_trim_rules`` on APPROVED rows, which is why
    this cannot be folded into the veto query above: a trim lives on a row
    whose ``risk_veto_rule`` is NULL and whose ``risk_approved`` is true.
    They are the same story ("risk refused something") told about
    different rows, so they are counted separately and never summed.

    Filtering happens in Python rather than in a JSONB predicate: the list
    is short (one window of one tenant's decisions), and a ``->>`` array
    containment expression here would be one more place for the field name
    to drift out of sync with ``runtime._reasoning_block``.
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    session_factory = async_session_factory()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AgentDecision.reasoning).where(
                        AgentDecision.risk_approved.is_(True),
                        AgentDecision.triggered_at >= cutoff,
                        AgentDecision.reasoning.is_not(None),
                        *tenant,
                    )
                )
            )
            .scalars()
            .all()
        )

    return count_trim_rules(rows)


def count_trim_rules(reasonings: Sequence[object]) -> list[TrimRuleRow]:
    """Tally ``reasoning.risk_trim_rules`` across decision rows.

    Split out from the query so the aggregation is testable without a
    database. Tolerant by construction: a row with no ``reasoning``, a
    non-dict, a missing key, or a non-string entry is skipped rather than
    raising — these rows are historical JSONB written by several
    generations of this code, and one malformed row must not empty the
    whole ledger.
    """
    counts: dict[str, int] = {}
    for reasoning in reasonings:
        if not isinstance(reasoning, dict):
            continue
        rules = reasoning.get("risk_trim_rules")
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if isinstance(rule, str) and rule:
                counts[rule] = counts.get(rule, 0) + 1

    # Ties break alphabetically so the ledger's row order is stable across
    # reloads — a scorecard that reshuffles on refresh reads as broken.
    return sorted(
        (TrimRuleRow(rule=r, count=c) for r, c in counts.items()),
        key=lambda t: (-t.count, t.rule),
    )


@dataclass
class VetoExemplar:
    """The single most extreme finalized ghost under one rule — the "story
    trade" (IMPL_REFUSAL_LEDGER.md §2.2): the council wanted it, risk said
    no, and here is what it was worth."""

    decision_id: str
    rule: str
    symbol: str
    side: str
    qty: int
    entry_price: float
    last_price: float | None
    ghost_pnl: float
    prevented_loss_usd: float
    is_option: bool
    occ_symbol: str | None
    bull_case: str
    bear_case: str
    rationale: str
    estimated_notional: float | None
    triggered_at: datetime
    horizon_days: int


async def build_veto_exemplar(rule: str, *, user_id: str) -> VetoExemplar | None:
    """Largest ``abs(ghost_pnl)`` among FINALIZED ghosts for ``rule`` —
    never the most recent. None when nothing under this rule has finalized
    yet (the caller renders that as "pending", not a missing rule)."""
    try:
        tenant = _tenant_filters(user_id)
    except _NoSuchTenant:
        return None
    session_factory = async_session_factory()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AgentDecision, GhostOutcome)
                    .join(GhostOutcome, GhostOutcome.decision_id == AgentDecision.id)
                    .where(
                        AgentDecision.risk_approved.is_(False),
                        AgentDecision.risk_veto_rule == rule,
                        GhostOutcome.status == "final",
                        GhostOutcome.ghost_pnl.is_not(None),
                        *tenant,
                    )
                )
            )
            .all()
        )
    if not rows:
        return None

    dec, ghost = max(rows, key=lambda pair: abs(float(pair[1].ghost_pnl)))
    p = dec.proposal or {}
    ghost_pnl = float(ghost.ghost_pnl)
    notional = p.get("estimatedNotional", p.get("estimated_notional"))
    return VetoExemplar(
        decision_id=str(dec.id),
        rule=rule,
        symbol=dec.symbol,
        side=str(ghost.side),
        qty=int(ghost.qty),
        entry_price=float(ghost.entry_price),
        last_price=float(ghost.last_price) if ghost.last_price is not None else None,
        ghost_pnl=ghost_pnl,
        prevented_loss_usd=round(max(0.0, -ghost_pnl), 2),
        is_option=bool(p.get("isOption", p.get("is_option", False))),
        occ_symbol=p.get("occSymbol") or p.get("occ_symbol"),
        bull_case=str(dec.bull_case or p.get("bullCase") or p.get("bull_case") or ""),
        bear_case=str(dec.bear_case or p.get("bearCase") or p.get("bear_case") or ""),
        rationale=str(p.get("rationale") or ""),
        estimated_notional=float(notional) if isinstance(notional, (int, float)) else None,
        triggered_at=dec.triggered_at,
        horizon_days=int(ghost.horizon_days),
    )
