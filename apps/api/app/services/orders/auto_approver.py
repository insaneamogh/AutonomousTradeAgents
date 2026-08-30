"""Auto-approver — lets the agent open a trade unattended, on a leash.

Per ``docs/PLAN_AUTO_APPROVE.md``: the agent currently drafts a proposal,
writes an audit row, and stops — every entry is human-gated. Called from
``ReconcilerFleet.tick()`` AFTER ``manage_positions_for_user`` (exits free
premium before entries consume it; do not reorder).

Eight gates, ALL of them, or nothing executes:

  1. ``AUTO_APPROVE_ENABLED`` env — the operator's master switch. Default
     OFF.
  2. Paper-mode, HARD-CODED. See the warning below — this must never
     become configurable.
  2b. This user's ``auto_approve_consent`` — the account owner's own
      in-app toggle (added on top of the plan's original design; mirrors
      live-trading's operator-env + per-connection-consent shape exactly).
  3. The regular US session is open right now (``is_us_market_open``).
  4. The proposal is fresh — age <= ``AUTO_APPROVE_MAX_AGE_MIN``. A stale
     thesis is not worth executing blind.
  5. Auto-approvals today (per user, per UTC day) < ``AUTO_APPROVE_MAX_PER_DAY``.
  6. Per-tick cap of ONE.
  7. The drawdown circuit breaker is not tripped for this user.

### Gate 2 is not configurable and must not become configurable

Refuse to auto-approve in live mode, unconditionally, even when
``AUTO_APPROVE_ENABLED=1``. Not a warning, not a config option — a hard
``return 0`` with a log line. The blast radius of this feature in paper is
a bad number on a dashboard; in live it is real money placed by a loop with
no human in it. This is deliberately hard-coded as::

    trading_mode() == "paper" and not env_flag("LIVE_TRADING_ENABLED")

Do not "generalise" this into an env-driven bypass, a per-user override, or
anything else that lets live mode route through here — a future edit that
makes this respect some new flag re-opens exactly the hole this plan closes.

### Gate 6 is a blast-radius bound, not an optimization

One order per 30-second tick means a bug that mis-reads the pending list
places ONE wrong order, and there are 30 seconds to notice — instead of
emptying the whole inbox in a single pass. Do not remove it as
over-engineering; it is the same reasoning as the exit agent's per-tick
consult cap.

### No second copy of the risk engine here

``execute_proposal`` re-runs the FULL deterministic risk gate against the
freshest broker state — that is CLAUDE.md §4.4's "the same number in two
places" trap, already solved once. This module adds ZERO new risk rules;
gates 3-7 above are scheduling / budget / consent bounds on WHEN and HOW
OFTEN we are allowed to ask ``execute_proposal`` to try, never a second
opinion on WHETHER a trade is safe. If ``execute_proposal`` reports
``risk_blocked``, the proposal stays pending — logged, never retried in a
loop. The condition may clear by the next tick on its own.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.core.ids import to_uuid as _to_uuid
from app.core.time import utc_now
from app.services.broker.broker_store import get_broker_store
from app.services.council.store import get_store
from app.services.orders.executor import execute_proposal
from app.services.orders.paper_broker import trading_mode
from engine.env import env_flag
from engine.features.market_calendar import is_us_market_open
from engine.risk import RiskCaps, load_db_risk_state

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.schemas.approvals import ApprovalProposalDto
    from app.services.broker.broker_store import BrokerConnectionRecord

logger = logging.getLogger("api.auto_approver")

_DEFAULT_MAX_PER_DAY = 5
_DEFAULT_MAX_AGE_MIN = 60


def _env_int(name: str, default: int) -> int:
    """Env override with the SAME fail-to-default contract as
    ``engine.risk.types._env_int``: a malformed value keeps the default and
    logs a warning rather than silently widening a bound. A typo must never
    widen an auto-approval bound.

    Kept as a small local copy rather than importing that module's
    underscore-private helper across the api/engine package boundary — the
    thing being reused is the CONTRACT (fail-to-default, never fail-open),
    not a threshold VALUE, so this isn't the CLAUDE.md §4.4 "same number in
    two places" trap that constrains options_min_volume; the two modules'
    actual env var names and defaults never overlap.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "auto_approver: ignoring malformed %s=%r — keeping default %r",
            name, raw, default,
        )
        return default


def _aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC rather than raising.

    ``proposed_at`` should already be tz-aware (Pydantic v2 round-trips
    ISO-8601 with offset, and every writer in this codebase uses
    ``utc_now()``) but a stale/legacy row must not crash the sweeper.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _resolve_paper_connection(user_id: str) -> BrokerConnectionRecord | None:
    """This user's own active Alpaca PAPER connection, or None.

    Auto-approve is Alpaca/paper-only — gate 2 already enforces the global
    paper-mode invariant; this resolves the ONE connection whose
    ``auto_approve_consent`` is the authority for gate 2b. Deliberately
    filters ``is_paper`` explicitly here rather than reusing
    ``broker_use.get_active_broker_connection`` (which has no ``is_paper``
    filter and would happily hand back a LIVE connection's consent flag —
    exactly the wrong row to read for a feature that must stay paper-only).
    """
    store = get_broker_store()
    conns = await store.list_connections(user_id)
    return next(
        (c for c in conns if c.broker == "alpaca" and c.is_paper and c.status == "active"),
        None,
    )


async def _auto_approvals_today(session_factory: async_sessionmaker, user_id: str) -> int:
    """Count of this user's auto-approved decisions since 00:00 UTC today."""
    uid = _to_uuid(user_id)
    if uid is None:
        return 0

    from sqlalchemy import func, select

    from engine.db.models import AgentDecision

    start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_factory() as session:
        stmt = (
            select(func.count())
            .select_from(AgentDecision)
            .where(
                AgentDecision.user_id == uid,
                AgentDecision.approval_mode == "auto",
                AgentDecision.user_responded_at.is_not(None),
                AgentDecision.user_responded_at >= start_of_day,
            )
        )
        return int((await session.execute(stmt)).scalar_one())


async def _stamp_auto_approval(
    session_factory: async_sessionmaker, *, user_id: str, proposal_id: str
) -> None:
    """Mark this decision row ``approval_mode='auto'`` — the only evidence
    in the audit log that this trade was placed with no human in the loop.

    Called AFTER ``execute_proposal`` succeeds. Verified end to end: neither
    ``Store.decide`` (``postgres_store.py``) nor ``finalize_execution_claim``
    (``execution_claim.py``) touch ``approval_mode`` — both write only
    ``user_response`` / ``user_responded_at`` / ``completed_at`` / (optionally)
    ``exit_mode``. So this stamp can't be clobbered by either of them, but it
    still runs AFTER execution succeeds on purpose: a row that never actually
    executed must never read as autonomous.
    """
    uid = _to_uuid(user_id)
    if uid is None:
        return

    from sqlalchemy import update

    from engine.db.models import AgentDecision

    async with session_factory() as session:
        await session.execute(
            update(AgentDecision)
            .where(
                AgentDecision.user_id == uid,
                AgentDecision.proposal["id"].astext == proposal_id,
            )
            .values(approval_mode="auto")
        )
        await session.commit()


async def auto_approve_for_user(
    *, user_id: str, session_factory: async_sessionmaker, caps: RiskCaps | None = None
) -> int:
    """One sweep for one user. Returns the number of proposals executed.

    Always 0 or 1 — gate 6 is a hard per-tick cap (see the module
    docstring). Never raises: a broker or DB failure mid-sweep is logged
    and swallowed so one user's trouble can't stop
    ``ReconcilerFleet.tick()`` from reconciling everyone else.
    """
    # Gate 1 — operator kill switch. Default OFF, and this is the normal
    # steady state for most of this feature's life, so stay silent here.
    if not env_flag("AUTO_APPROVE_ENABLED"):
        return 0

    # Gate 2 — HARD-CODED paper-only. See the module docstring: this must
    # never become configurable, so it stays a literal boolean expression,
    # not a lookup against any table of "safe modes".
    if not (trading_mode() == "paper" and not env_flag("LIVE_TRADING_ENABLED")):
        logger.warning(
            "auto_approver: refusing for user=%s — not in safe paper mode "
            "(trading_mode=%s live_trading_enabled=%s)",
            user_id, trading_mode(), env_flag("LIVE_TRADING_ENABLED"),
        )
        return 0

    # Gate 2b — the account owner's own in-app consent. Second key of the
    # two-key gate; the operator env alone is not enough.
    conn = await _resolve_paper_connection(user_id)
    if conn is None or not conn.auto_approve_consent:
        logger.info(
            "auto_approver: no auto-approve consent for user=%s — skipping "
            "(connection=%s)",
            user_id, "none" if conn is None else conn.id,
        )
        return 0

    now = utc_now()

    # Gate 3 — regular US session only. Pre/post-market feeds are thin
    # enough that a single odd-lot can manufacture a false signal, and
    # that's the SAME reasoning the scanner already uses for this gate.
    if not is_us_market_open(now):
        return 0

    store = get_store()
    pending: list[ApprovalProposalDto] = await store.list_pending(user_id)

    # Gate 4 — drop stale proposals. list_pending() already excludes
    # EXPIRED ones (a much longer, end-of-day window); this is a tighter,
    # auto-approval-specific freshness bound on top of that.
    max_age = timedelta(minutes=_env_int("AUTO_APPROVE_MAX_AGE_MIN", _DEFAULT_MAX_AGE_MIN))
    eligible = [p for p in pending if now - _aware(p.proposed_at) <= max_age]
    if not eligible:
        return 0

    max_per_day = _env_int("AUTO_APPROVE_MAX_PER_DAY", _DEFAULT_MAX_PER_DAY)

    try:
        # Gate 5 — daily budget.
        today_count = await _auto_approvals_today(session_factory, user_id)
        if today_count >= max_per_day:
            logger.info(
                "auto_approver: daily budget reached (%d/%d) for user=%s",
                today_count, max_per_day, user_id,
            )
            return 0

        # Gate 6 — per-tick cap of ONE. Oldest-proposed first: the fairest
        # queueing order, and also the one closest to aging out anyway.
        proposal = min(eligible, key=lambda p: _aware(p.proposed_at))

        # Gate 7 — the drawdown circuit breaker. This reads the SAME
        # persisted halt state ``execute_proposal`` re-checks inside the
        # real risk gate; checking it here too just avoids spending the
        # daily budget / per-tick slot on an attempt that would refuse
        # anyway. Not a second risk rule — a read of one that already exists.
        db_state = await load_db_risk_state(session_factory, user_id=user_id)
        if db_state.drawdown_halted:
            logger.info(
                "auto_approver: breaker tripped for user=%s — refusing", user_id
            )
            return 0

        result = await execute_proposal(
            user_id=user_id,
            proposal_id=proposal.id,
            risk_caps=caps,
            exit_mode="agent",
        )
    except Exception:
        # A broker or DB hiccup mid-sweep must not kill the fleet tick for
        # every other user. ReconcilerFleet.tick() wraps this call too, but
        # this function's own contract is "never raises" regardless.
        logger.exception(
            "auto_approver: sweep failed for user=%s — refusing this tick", user_id
        )
        return 0

    if result.risk_blocked:
        # Stays pending on purpose. The condition may clear by the next
        # tick; retrying it in a loop here would turn one veto into a hot
        # loop (CLAUDE.md §4.4 / PLAN_AUTO_APPROVE.md §5).
        logger.info(
            "auto_approver: risk_blocked proposal=%s user=%s rule=%s — stays pending",
            proposal.id, user_id, result.risk_veto_rule,
        )
        return 0

    await _stamp_auto_approval(session_factory, user_id=user_id, proposal_id=proposal.id)
    logger.info(
        "auto_approver: AUTO-APPROVED proposal=%s user=%s symbol=%s",
        proposal.id, user_id, proposal.symbol,
    )
    return 1
