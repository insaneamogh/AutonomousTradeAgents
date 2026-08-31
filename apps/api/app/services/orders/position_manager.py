"""Position manager — the agent-side close path for delegated exits.

The user approved the entry AND the disclosed exit plan; this worker
executes the parts a broker-side bracket can't:

  TIME-STOP      the position is older than the proposal's
                 ``time_stop_days`` → close. Brackets only cover price
                 levels; "exit after N days if neither hit" lives here.
  SIGNAL EXIT    today's council decision for the SAME symbol came out
                 SELL → close early. The council proposes (the signal);
                 this deterministic code disposes — no LLM output touches
                 the close mechanics, exactly like entries.
  PREMIUM EXIT   (options only) the contract's own premium hit the
                 take-profit or the stop. Alpaca cannot bracket a
                 single-leg option — OrderClass allows only simple/mleg
                 for us_option — so the broker-side stop/target that
                 protects every equity entry is structurally unavailable
                 and has to live here. Checked BEFORE the time stop: a
                 position that already hit its target should not sit for
                 two more sessions waiting on the calendar. See
                 ``engine.options.exits``.
  ESCALATION     (options only, docs/IMPL_OPTIONS_AGENTS.md §5) — when
                 none of the above fired (the deterministic checks all
                 said "keep holding") AND the trailing ratchet reports a
                 MATERIAL change (just armed / peak advanced ≥15pp since
                 the last escalation / price within 10pp of the trail
                 line / DTE≤5), a single LLM gets ONE guarded chance to
                 tighten protection, bank the take-profit, close early, or
                 scale in — never to loosen anything. This is a SECONDARY
                 layer on top of the ratchet, which keeps running
                 regardless of what the escalation agent does or whether
                 it runs at all; see ``trading_agents.options.escalation``
                 for the full design rationale and the fail-safe
                 (error/timeout/MOCK mode → nothing changes).

Scope rules:
  - ONLY decisions with ``exit_mode='agent'``. Manual-mode positions are
    never touched, no matter what.
  - Closes route through the SAME deterministic risk gate as entries
    (a close is always allowed even under a drawdown halt — de-risking is
    always permitted, whether that's a SELL flattening a long or a BUY
    covering a short).
  - The close side is derived from the HELD position, not assumed: a long
    closes with a SELL, a short closes with a BUY-to-cover. Placing the
    wrong side would increase the position instead of closing it.
  - Resting bracket children are canceled first, or the broker would
    reject the market close order for unavailable qty.
  - ``close_reason`` is stamped immediately ('agent_time' /
    'agent_signal'); ``closed_at`` + ``realized_pnl`` land when order_sync
    confirms the fill. A push tells the user what happened and why.

Runs per user from the reconciler fleet tick. Postgres-only (the decision
rows ARE the position ledger).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select, text, update

from app.services.broker.broker_use import with_broker_client
from app.services.orders.executor import _build_risk_context
from app.services.orders.order_store import persist_linked_order_submit, persist_order_result
from engine.options.exits import RatchetOutcome, option_exit_signal, option_ratchet_signal
from engine.options.expiry import dte
from engine.risk import RiskCaps

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger("api.position_manager")

_CLOSE_REASON_LABEL = {
    "agent_time": "time stop reached",
    "agent_signal": "council flipped to SELL",
    "agent_expiry": "closing ahead of expiry",
    "option_take_profit": "premium take-profit hit",
    "option_stop_loss": "premium stop-loss hit",
    "option_trail_stop": "trailing stop hit",
}

# Mirrors the drafter / ghost evaluator horizon map — used only when an
# old proposal predates the time_stop_days field.
_FALLBACK_TIME_STOP_BY_HORIZON = {"intraday": 1, "short": 5, "mid": 10, "long": 20}


async def manage_positions_for_user(
    *,
    user_id: str,
    session_factory: async_sessionmaker,
    caps: RiskCaps | None = None,
    escalation_budget: Any | None = None,
    llm: Any | None = None,
    guard: Any | None = None,
) -> int:
    """One pass: close every agent-managed position whose exit condition
    fired. Returns the number of closes initiated.

    ``escalation_budget``/``llm``/``guard`` are injectable purely for the
    escalation loop (see the ESCALATION scope rule above) — production
    leaves all three ``None`` and gets sane per-call defaults (a
    fresh, one-shot ``EscalationBudget`` and lazily-constructed
    ``LLM()``/``ToolGuard()`` instances, both cheap to build — see
    ``trading_agents.options.tools.guard.ToolGuard``'s own "cheap to
    construct" docstring). ``ReconcilerFleet.tick()`` constructs ONE
    ``EscalationBudget`` per tick and threads the SAME instance into every
    user's call so the "1 escalation per fleet tick" cap
    (docs/IMPL_OPTIONS_AGENTS.md §5.1) is enforced across the WHOLE fleet,
    not per user. Tests inject fakes for all three so this path never
    needs a real Anthropic key or a real broker to exercise.
    """
    from engine.db.models import AgentDecision

    uid = uuid.UUID(user_id)
    now = datetime.now(UTC)

    async with session_factory() as session:
        stmt = (
            select(AgentDecision)
            .where(AgentDecision.user_id == uid)
            .where(AgentDecision.user_response == "approved")
            .where(AgentDecision.fill_qty.is_not(None))
            .where(AgentDecision.closed_at.is_(None))
            .where(AgentDecision.exit_mode == "agent")
        )
        open_decisions = (await session.execute(stmt)).scalars().all()

        if not open_decisions:
            return 0

        caps = caps or RiskCaps.from_env()
        # Only pay for the broker read when an option is actually open.
        has_option = any(
            bool((d.proposal or {}).get("isOption", (d.proposal or {}).get("is_option", False)))
            for d in open_decisions
        )
        option_pl_pct = await _option_pl_pct_by_symbol(user_id) if has_option else {}

        # Lazily constructed, and ONLY when this user actually has an open
        # option position — an equity-only book never touches
        # `trading_agents` at all. Constructed ONCE per user-tick and
        # reused across every decision below; both are cheap, stateless-
        # per-call resolvers (see their own docstrings), not per-decision
        # state.
        escalation_llm = llm
        escalation_guard = guard
        if has_option and (escalation_llm is None or escalation_guard is None):
            from trading_agents.llm import LLM
            from trading_agents.options.tools.guard import ToolGuard

            escalation_llm = escalation_llm if escalation_llm is not None else LLM()
            escalation_guard = escalation_guard if escalation_guard is not None else ToolGuard()

        budget = escalation_budget
        if budget is None:
            from trading_agents.options.escalation import EscalationBudget

            budget = EscalationBudget()

        closes = 0
        for decision in open_decisions:
            # Computed ONCE per decision per tick and threaded into
            # `_exit_reason` below rather than recomputed there — the
            # peak-persistence write after `_exit_reason` returns needs the
            # SAME outcome, and a second independent computation could only
            # ever agree with the first (both are pure), so the point of
            # computing it once here is to have exactly one value in scope
            # for both the close decision and the write, not a possible
            # accuracy difference.
            ratchet_outcome = _ratchet_outcome_for(decision, option_pl_pct, caps)
            reason = await _exit_reason(
                session, decision, now, caps=caps, option_pl_pct=option_pl_pct,
                ratchet_outcome=ratchet_outcome,
            )
            if ratchet_outcome is not None and ratchet_outcome.peak_advanced:
                # Persisted regardless of whether this tick ALSO closes the
                # position — the peak at the moment of close is still real
                # audit history. Write only on advancement: at a 30s tick
                # cadence across a whole session that is ~10 writes instead
                # of ~800 (PLAN_EXIT_AGENT.md §4).
                try:
                    await _persist_option_exit_peak(
                        session_factory, decision=decision, outcome=ratchet_outcome
                    )
                except Exception:
                    logger.exception(
                        "position_manager: failed to persist the option-exit "
                        "peak for %s (%s) — continuing without it",
                        decision.symbol, decision.id,
                    )
            if reason is None:
                # The deterministic ratchet/stop/time/expiry checks all
                # said "keep holding" this tick. `ratchet_outcome is None`
                # for a non-option decision, a non-ratchet-managed option
                # (caps.options_ratchet_enabled off), or one with no OCC
                # symbol — none of those are escalation candidates at all,
                # since "armed"/"trail line" only mean anything when the
                # ratchet is the thing managing this position.
                if ratchet_outcome is not None:
                    try:
                        dte_value = _option_dte(decision, now)
                        await maybe_escalate_option_position(
                            decision=decision,
                            ratchet_outcome=ratchet_outcome,
                            dte=dte_value,
                            now=now,
                            budget=budget,
                            llm=escalation_llm,
                            guard=escalation_guard,
                            caps=caps,
                            session_factory=session_factory,
                        )
                    except Exception:
                        logger.exception(
                            "position_manager: escalation check failed for "
                            "%s (%s) — continuing; the deterministic ratchet "
                            "keeps running untouched",
                            decision.symbol, decision.id,
                        )
                continue
            # Re-entrance guard: a SELL we placed on a prior tick may still
            # be pending/accepted (closed_at only lands when it FILLS, via
            # order_sync). Without this check the manager re-runs risk +
            # cancel + place AND re-fires the "agent closing" push every
            # tick until the fill confirms. Skip if an exit is already live.
            if await _has_in_flight_close(session, decision.id):
                logger.debug(
                    "position_manager: close already in flight for %s (%s) — skipping",
                    decision.symbol, decision.id,
                )
                continue
            try:
                initiated = await _close_position(
                    session_factory,
                    user_id=user_id,
                    decision=decision,
                    reason=reason,
                )
            except Exception:
                logger.exception(
                    "position_manager: close failed for %s (%s)",
                    decision.symbol, decision.id,
                )
                continue
            if initiated:
                closes += 1
        return closes


async def _option_pl_pct_by_symbol(user_id: str) -> dict[str, float]:
    """Broker-reported unrealized P&L percent, keyed by OCC symbol.

    Fetched ONCE per user pass rather than per position: the premium exit
    needs a current mark, and a broker round trip inside the per-decision
    loop would multiply the API calls by the size of the book.

    The broker's own number is used rather than a mark computed here. It
    is the same figure the user sees in Alpaca, it is already scaled by
    the contract multiplier, and deriving our own from a 15-minute-delayed
    quote would let a stale tick close a live position.

    A broker failure returns {} — every option then reports no mark, and
    ``option_exit_signal`` holds. Degrading to "don't close" is the only
    safe direction: the time stop and the expiry sweep still run, so a
    position is never left unmanaged, merely un-price-stopped for a tick.
    """
    try:
        async with with_broker_client(user_id, broker="alpaca") as (broker, _conn):
            positions = await broker.list_positions()
    except Exception:
        logger.warning(
            "position_manager: could not read broker positions for premium exits "
            "(user=%s) — holding every option this tick",
            user_id, exc_info=True,
        )
        return {}
    return {
        p.symbol.upper(): float(p.unrealized_pl_pct)
        for p in positions
        if p.is_option and p.unrealized_pl_pct is not None
    }


def _ratchet_outcome_for(
    decision,
    option_pl_pct: dict[str, float] | None,
    caps: RiskCaps,
) -> RatchetOutcome | None:
    """The ``RatchetOutcome`` for one decision, or ``None`` when it is not a
    ratchet candidate at all (not an option, no OCC symbol on the
    proposal, or ``caps.options_ratchet_enabled`` is off) — callers fall
    back to ``option_exit_signal`` in that case.

    Pure given its inputs: reads the persisted peak from
    ``decision.reasoning["option_exit"]["peak_pl_pct"]`` (already in hand
    on the loaded row — no extra query) and the current mark from
    ``option_pl_pct`` (already fetched once per user tick by
    ``_option_pl_pct_by_symbol``). No I/O here.
    """
    if not caps.options_ratchet_enabled:
        return None
    proposal = decision.proposal or {}
    if not bool(proposal.get("isOption", proposal.get("is_option", False))):
        return None
    occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
    if not occ:
        return None
    reasoning = getattr(decision, "reasoning", None) or {}
    existing_state = reasoning.get("option_exit") or {}
    return option_ratchet_signal(
        unrealized_pl_pct=(option_pl_pct or {}).get(str(occ).upper()),
        peak_pl_pct=existing_state.get("peak_pl_pct"),
        arm_pct=caps.options_trail_arm_pct,
        # RiskCaps stores this as a PERCENT of the peak (30.0); the pure
        # function wants a FRACTION (0.30) — see its own docstring.
        giveback_frac=caps.options_trail_giveback_pct / 100.0,
        hard_take_profit_pct=caps.options_hard_take_profit_pct,
        stop_loss_pct=caps.options_stop_loss_pct,
    )


def _option_exit_peak_update_stmt(*, decision_id, payload: dict) -> tuple[object, dict]:
    """Builds the parameterized ``jsonb_set`` UPDATE for the high-water
    mark — split out from the execute wrapper below so the emitted SQL and
    its bound params are directly assertable in a test with no live
    Postgres involved (this suite has no live-DB harness at all; every
    existing test in this package mocks the session — see
    ``fable5findings.md`` for why jsonb_set's actual runtime behavior is
    verified by reasoning about documented Postgres semantics plus this
    statement's shape, not by executing it against a real database).

    ``COALESCE`` is REQUIRED: ``reasoning`` is nullable and
    ``jsonb_set(NULL, ...)`` returns ``NULL`` in Postgres, which would
    silently blank the whole column instead of writing to it.
    ``create_missing=true`` (the literal ``true`` 4th argument) creates the
    ``option_exit`` key the first time; every write after that replaces it.

    This updates ONLY the ``option_exit`` key — ``jsonb_set`` never touches
    sibling keys in the same JSONB column (``contract_funnel``,
    ``strategy_fit``), which is the entire reason this exists instead of a
    plain ``UPDATE ... SET reasoning = :payload``.
    """
    return (
        text(
            "UPDATE agent_decisions "
            "SET reasoning = jsonb_set("
            "COALESCE(reasoning, '{}'::jsonb), '{option_exit}', "
            "CAST(:payload AS jsonb), true"
            ") "
            "WHERE id = :id"
        ),
        {"payload": json.dumps(payload, default=str), "id": str(decision_id)},
    )


async def _persist_option_exit_peak(
    session_factory: async_sessionmaker,
    *,
    decision,
    outcome: RatchetOutcome,
) -> None:
    """Writes the new high-water mark. Called only when
    ``outcome.peak_advanced`` — see the call site in
    ``manage_positions_for_user``.

    The payload MERGES the fields this module owns (``peak_pl_pct``,
    ``armed``, ``trail_line_pct``) over whatever already lives under the
    ``option_exit`` key, rather than replacing it outright. Nothing in this
    module writes ``consult_date``/``consults``/``last_consult_at``/``log``
    yet (that is the exit agent's own future write, a separate piece of
    work) — but if it ever does, a ratchet-only tick must not silently wipe
    that consult history the next time the peak advances without a
    consult happening on the same tick.
    """
    existing_state = (getattr(decision, "reasoning", None) or {}).get("option_exit") or {}
    payload = {
        **existing_state,
        "version": 1,
        "peak_pl_pct": outcome.peak_pl_pct,
        "armed": outcome.armed,
        "trail_line_pct": outcome.trail_line_pct,
    }
    stmt, params = _option_exit_peak_update_stmt(decision_id=decision.id, payload=payload)
    async with session_factory() as session:
        await session.execute(stmt, params)
        await session.commit()


def _coerce_expiry_date(value: object) -> date | None:
    """Best-effort parse of a persisted proposal's expiry field — JSONB
    round-trips a Python ``date`` as an ISO-8601 string. Returns None on
    anything unparseable rather than raising, since a malformed/missing
    expiry must not crash the sweep for every OTHER user's positions."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _option_dte(decision, now: datetime) -> int | None:
    """Days to expiry for one decision's stored proposal, or ``None`` when
    it isn't an option / has no parseable expiry. Used only by the
    escalation trigger below — ``sweep_expiring_options_for_user`` (the
    UNCONDITIONAL DTE≤2 force-close) computes this independently in its
    own query and must keep doing so; this helper does not replace it."""
    proposal = decision.proposal or {}
    if not bool(proposal.get("isOption", proposal.get("is_option", False))):
        return None
    expiry = _coerce_expiry_date(proposal.get("expiryDate", proposal.get("expiry_date")))
    if expiry is None:
        return None
    return dte(expiry, now)


async def maybe_escalate_option_position(
    *,
    decision,
    ratchet_outcome: RatchetOutcome,
    dte: int | None,
    now: datetime,
    budget: Any,
    llm: Any,
    guard: Any,
    caps: RiskCaps,
    session_factory: async_sessionmaker,
) -> Any:
    """Thin, lazily-importing wrapper around
    ``trading_agents.options.escalation.maybe_escalate`` — see that
    module for the trigger conditions, rate limits, the single-agent
    design decision, and the fail-safe. Lazy import matches this file's
    OWN convention for reaching into sibling packages (``broker.types``,
    ``engine.db.models`` are imported the same way, inline, throughout
    this file) and ``apps/api``'s existing precedent for calling INTO
    ``trading_agents`` (``app.services.council.scheduler`` already does
    this, also lazily, for the equity council).
    """
    from trading_agents.options.escalation import maybe_escalate

    return await maybe_escalate(
        decision=decision,
        ratchet_outcome=ratchet_outcome,
        dte=dte,
        now=now,
        budget=budget,
        llm=llm,
        guard=guard,
        caps=caps,
        session_factory=session_factory,
    )


async def sweep_expiring_options_for_user(
    *,
    user_id: str,
    session_factory: async_sessionmaker,
    caps: RiskCaps | None = None,
) -> int:
    """One pass: force-close every agent-managed OPTION position within
    ``caps.options_expiry_sweep_dte`` days of expiry.

    Automatic by default, not a surfaced decision — matches "deterministic
    code disposes." Per docs/OPTIONS_PLAN.md §2.6, an open option
    approaching expiry must never be left to Alpaca's own auto-exercise (a
    $500 option silently becoming a $30,000 share position overnight) or
    to simply expire worthless through inattention. Silence must not be a
    decision.

    Mirrors ``manage_positions_for_user``'s exact shape: same
    session-per-user pattern, same re-entrance guard
    (``_has_in_flight_close``), same ``_close_position`` path — only the
    exit condition (DTE, not time-stop/signal) and the close reason
    (``"agent_expiry"``) differ. Deliberately does NOT reuse
    ``_exit_reason`` — expiry is an unconditional trigger, not one more
    condition in that function's time-stop/signal-exit branching.
    """
    from engine.db.models import AgentDecision

    caps = caps or RiskCaps.from_env()
    uid = uuid.UUID(user_id)
    now = datetime.now(UTC)

    async with session_factory() as session:
        stmt = (
            select(AgentDecision)
            .where(AgentDecision.user_id == uid)
            .where(AgentDecision.user_response == "approved")
            .where(AgentDecision.fill_qty.is_not(None))
            .where(AgentDecision.closed_at.is_(None))
            .where(AgentDecision.exit_mode == "agent")
        )
        open_decisions = (await session.execute(stmt)).scalars().all()

        if not open_decisions:
            return 0

        closes = 0
        for decision in open_decisions:
            proposal = decision.proposal or {}
            is_option = bool(proposal.get("isOption", proposal.get("is_option", False)))
            if not is_option:
                continue

            expiry = _coerce_expiry_date(
                proposal.get("expiryDate", proposal.get("expiry_date"))
            )
            if expiry is None:
                logger.warning(
                    "sweep_expiring_options_for_user: %s (%s) is flagged "
                    "is_option but has no parseable expiry — skipping the "
                    "sweep check rather than closing blind.",
                    decision.symbol, decision.id,
                )
                continue

            if dte(expiry, now) > caps.options_expiry_sweep_dte:
                continue

            if await _has_in_flight_close(session, decision.id):
                logger.debug(
                    "sweep_expiring_options_for_user: close already in "
                    "flight for %s (%s) — skipping",
                    decision.symbol, decision.id,
                )
                continue

            try:
                initiated = await _close_position(
                    session_factory,
                    user_id=user_id,
                    decision=decision,
                    reason="agent_expiry",
                )
            except Exception:
                logger.exception(
                    "sweep_expiring_options_for_user: close failed for %s (%s)",
                    decision.symbol, decision.id,
                )
                continue
            if initiated:
                closes += 1
        return closes


async def close_position_now(
    *,
    user_id: str,
    decision_id: str,
    session_factory: async_sessionmaker,
    reason: str = "user_manual",
) -> dict:
    """User-initiated close from the app. Works for BOTH manual-mode
    positions (the user closes when they choose) AND agent-mode (the user
    overrides the agent early). Same risk gate + bracket-cancel + persist +
    push as the agent path — only the ``reason`` differs.

    Dispatches to ``cancel_pending_order_now`` when the entry never filled
    — "close" is the one user-facing verb for "stop this trade", whether
    that means flattening a live position or cancelling an order that's
    still working at the broker. Before this, an approved-but-unfilled
    proposal had no way to be stopped at all: the ``fill_qty`` check below
    used to just refuse with ``no_open_position``, which is technically
    true and completely unhelpful outside market hours, when an order can
    sit unfilled for hours.

    Returns ``{closed: bool, error: str | None}``. ``error`` is one of
    not_found / not_owner / already_closed / no_open_position /
    no_pending_order / close_in_flight / risk_vetoed.
    """
    import os

    from engine.db.models import AgentDecision

    # Positions live in Postgres (decisions ARE the ledger). MockStore dev
    # mode has no position to close.
    if os.environ.get("USE_POSTGRES", "").strip().lower() not in ("1", "true", "yes", "on"):
        return {"closed": False, "error": "not_found"}

    try:
        uid = uuid.UUID(user_id)
        did = uuid.UUID(decision_id)
    except (ValueError, TypeError):
        return {"closed": False, "error": "not_found"}

    async with session_factory() as session:
        decision = await session.get(AgentDecision, did)
        if decision is None:
            return {"closed": False, "error": "not_found"}
        if decision.user_id != uid:
            # Ownership check — a user can never close another user's position.
            return {"closed": False, "error": "not_owner"}
        if decision.closed_at is not None:
            return {"closed": False, "error": "already_closed"}
        if not decision.fill_qty:
            return await cancel_pending_order_now(
                session, session_factory, user_id=user_id, decision=decision
            )
        if await _has_in_flight_close(session, decision.id):
            return {"closed": False, "error": "close_in_flight"}

    initiated = await _close_position(
        session_factory, user_id=user_id, decision=decision, reason=reason
    )
    return {"closed": initiated, "error": None if initiated else "risk_vetoed"}


async def cancel_pending_order_now(
    session,
    session_factory: async_sessionmaker,
    *,
    user_id: str,
    decision,
) -> dict:
    """Cancel an approved order that hasn't filled yet.

    Finds the newest ``orders`` row for this decision still in an open
    broker state and cancels it there. Cancelling Alpaca's bracket PARENT
    (the entry) takes its OCO stop/target children with it — those don't
    exist as live orders at the broker until the parent fills, so there is
    nothing separate to cancel.

    The decision row's ``user_response`` stays ``'approved'`` — the user
    really did approve it, that's the audit fact. What changes is the
    ORDER's terminal status, which is what ``list_open_positions`` reads
    to decide whether an approved-but-unfilled row is still worth
    showing.
    """
    from sqlalchemy import desc, select

    from app.services.orders.order_sync import OPEN_ORDER_STATUSES
    from engine.db.models import Order

    stmt = (
        select(Order)
        .where(Order.agent_decision_id == decision.id)
        .where(Order.status.in_(OPEN_ORDER_STATUSES))
        .order_by(desc(Order.submitted_at))
        .limit(1)
    )
    order_row = (await session.execute(stmt)).scalar_one_or_none()
    if order_row is None or order_row.broker_order_id is None:
        return {"closed": False, "error": "no_pending_order"}

    try:
        async with with_broker_client(user_id, broker="alpaca") as (broker, _conn):
            canceled = await broker.cancel_order(order_row.broker_order_id)
    except Exception:
        logger.exception(
            "position_manager: cancel failed for %s order=%s",
            decision.symbol, order_row.broker_order_id,
        )
        return {"closed": False, "error": "risk_vetoed"}

    new_status = (
        canceled.status.value if hasattr(canceled.status, "value") else str(canceled.status)
    )
    async with session_factory() as write_session:
        await write_session.execute(
            update(Order).where(Order.id == order_row.id).values(status=new_status)
        )
        await write_session.commit()

    logger.info(
        "position_manager: cancelled unfilled order for %s user=%s (broker_order=%s → %s)",
        decision.symbol, user_id, order_row.broker_order_id, new_status,
    )
    return {"closed": True, "error": None}


async def _has_in_flight_close(session, decision_id) -> bool:
    """True if a close order for this decision is already pending/accepted
    at the broker — i.e. a close is in flight and we must not re-submit.

    Not filtered by side: a short's close is a BUY-to-cover, not a SELL, so
    a ``side == "SELL"`` filter would be blind to it and risk a double
    close-submit on a slow tick. Any open order tied to this decision past
    entry-resolution is a close attempt, whichever side it placed as.
    """
    from app.services.orders.order_sync import IN_FLIGHT_STATUSES
    from engine.db.models import Order

    stmt = (
        select(Order.id)
        .where(Order.agent_decision_id == decision_id)
        .where(Order.status.in_(IN_FLIGHT_STATUSES))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _exit_reason(
    session,
    decision,
    now: datetime,
    *,
    caps: RiskCaps | None = None,
    option_pl_pct: dict[str, float] | None = None,
    ratchet_outcome: RatchetOutcome | None = None,
) -> str | None:
    """Which exit condition fired, if any. Deterministic reads only.

    Order matters. The premium exit is checked FIRST for options: a
    contract that already hit its target should not sit two more sessions
    waiting for the calendar time stop, and one through its stop should not
    keep bleeding theta for the same reason.

    ``ratchet_outcome``, when given, is the CALLER'S already-computed
    ``RatchetOutcome`` for this decision (``manage_positions_for_user``
    computes it once per tick and reuses it here and for the peak-write
    that follows). When omitted — every direct-call site in this test file
    included — it is computed fresh from ``decision``/``option_pl_pct``,
    which is pure and gives the identical result; the parameter exists to
    avoid a second I/O-free computation in the hot path, not because a
    second computation would disagree.
    """
    proposal = decision.proposal or {}
    caps = caps or RiskCaps.from_env()

    # 0. Premium exit (options only). No broker mark -> no signal, and the
    #    time stop below still applies; a missing price never closes.
    if bool(proposal.get("isOption", proposal.get("is_option", False))):
        if caps.options_ratchet_enabled:
            outcome = (
                ratchet_outcome if ratchet_outcome is not None
                else _ratchet_outcome_for(decision, option_pl_pct, caps)
            )
            if outcome is not None and outcome.action == "CLOSE":
                logger.info(
                    "position_manager: %s fired for %s (%s) — %s",
                    outcome.reason, decision.symbol,
                    proposal.get("occSymbol") or proposal.get("occ_symbol"), outcome.detail,
                )
                return outcome.reason
        else:
            occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
            if occ:
                signal = option_exit_signal(
                    unrealized_pl_pct=(option_pl_pct or {}).get(str(occ).upper()),
                    take_profit_pct=caps.options_take_profit_pct,
                    stop_loss_pct=caps.options_stop_loss_pct,
                )
                if signal is not None:
                    logger.info(
                        "position_manager: %s fired for %s (%s) — %s",
                        signal.reason, decision.symbol, occ, signal.detail,
                    )
                    return signal.reason

    # 1. Time stop — Phase 0 calendar days, consistent with PDT/idempotency.
    time_stop_days = int(
        proposal.get("timeStopDays")
        or _FALLBACK_TIME_STOP_BY_HORIZON.get(str(decision.horizon), 5)
    )
    entered_at = decision.user_responded_at or decision.triggered_at
    if entered_at is not None:
        held_days = (now.date() - entered_at.date()).days
        if held_days >= time_stop_days:
            return "agent_time"

    # 2. Signal exit — a NEWER council decision on this symbol says SELL.
    from engine.db.models import AgentDecision

    newer_sell_stmt = (
        select(AgentDecision.id)
        .where(AgentDecision.user_id == decision.user_id)
        .where(AgentDecision.symbol == decision.symbol)
        .where(AgentDecision.id != decision.id)
        .where(AgentDecision.triggered_at > decision.triggered_at)
        .where(AgentDecision.final_action == "SELL")
        .order_by(desc(AgentDecision.triggered_at))
        .limit(1)
    )
    if (await session.execute(newer_sell_stmt)).scalar_one_or_none() is not None:
        return "agent_signal"

    return None


async def _close_position(
    session_factory: async_sessionmaker,
    *,
    user_id: str,
    decision,
    reason: str,
) -> bool:
    """Risk-gate → cancel resting legs → order → persist → notify.

    The close side is derived from the HELD position, never assumed: a
    long (positive qty) closes with a SELL, a short (negative qty) closes
    with a BUY-to-cover. Before this, every close hardcoded SELL — which
    for a short doesn't silently increase it (the risk engine's own
    ``held_long_qty`` sees 0 long shares held, so that SELL reads as
    "opening a fresh short" and ``forbid_short_phase_0``/``shortable_check``
    veto it — the latter unconditionally, since this call never set
    ``shortable``/``easy_to_borrow``). The observable failure was worse than
    silent: a short could never be closed through this path AT ALL, agent
    or manual — every attempt logged "close VETOED" forever.

    An OPTION position closes on its own branch, mirroring the shape of
    that same short-side fix: Phase A never holds a short option leg to
    cover, so the broker side is always SELL_TO_CLOSE, never a "buy to
    cover"; the order is always LIMIT (never MARKET — docs/OPTIONS_PLAN.md
    explicitly recommends against market orders on a 15-min-delayed
    indicative feed), priced off the freshly-fetched broker position's own
    mark — ``_build_risk_context`` just called ``broker.list_positions()``
    moments earlier, which is as fresh a quote as this codebase has for a
    position's current value, the same source the equity branch above
    already uses for its own ``last_price``.

    ``orders.side`` itself always stays plain "BUY"/"SELL" (the DB column
    is 4 chars wide and pre-dates options) — the open/close nuance for an
    options order lives in the separate ``option_action`` column, not in
    ``side``. The BROKER-wire side (``Side.SELL_TO_CLOSE``) is a distinct
    value from the DB-column side ("SELL") for exactly this reason.
    """
    from broker.types import OccSymbol, OrderRequest, OrderType, Side, TimeInForce
    from engine.options.contracts import contract_type_of, to_risk_proposal
    from engine.risk import OptionLegDetails, RiskProposal, evaluate
    from engine.risk import Side as RiskSide

    qty = int(decision.fill_qty or 0)
    if qty <= 0:
        return False
    symbol = decision.symbol.upper()
    client_order_id = f"agent-close-{decision.id}"
    # getattr, not decision.proposal — some callers (older fixtures, a
    # minimal decision-like object) may not carry a proposal attribute at
    # all; treat that exactly like an empty proposal rather than crashing.
    stored_proposal = getattr(decision, "proposal", None) or {}

    # ``symbol`` is the UNDERLYING on an options decision (that is what
    # agent_decisions.symbol stores, and what the cron dedup, ghost marking
    # and the UI all read). Everything that talks to the BROKER — the
    # open-position match, OCC parsing, resting-order cancel, and the close
    # order itself — must use the contract. Alpaca keys option positions by
    # OCC, so matching on the underlying finds nothing and an agent-managed
    # option could never be closed.
    _occ_stored = stored_proposal.get("occSymbol") or stored_proposal.get("occ_symbol")
    wire_symbol = str(_occ_stored).upper() if _occ_stored else symbol

    async with with_broker_client(user_id, broker="alpaca") as (broker, conn):
        risk_ctx = await _build_risk_context(broker, user_id=user_id)
        held = next(
            (
                p for p in risk_ctx.open_positions
                if p.symbol.upper() == wire_symbol and p.qty != 0
            ),
            None,
        )
        is_option = held.is_option if held is not None else bool(
            stored_proposal.get("isOption", stored_proposal.get("is_option", False))
        )
        multiplier = 1

        if is_option:
            multiplier = (
                held.multiplier if held is not None
                else int(stored_proposal.get("multiplier", 1) or 1)
            )
            db_side = "SELL"
            broker_close_side = Side.SELL_TO_CLOSE
            order_type = OrderType.LIMIT
            last_price = (
                held.market_value / (held.qty * multiplier)
                if held is not None and held.qty != 0
                else (float(decision.fill_avg_price or 0) or 1.0)
            )
            occ = OccSymbol.try_parse(wire_symbol)
            option = OptionLegDetails(
                underlying_symbol=occ.underlying if occ is not None else symbol,
                occ_symbol=wire_symbol,
                contract_type=(
                    contract_type_of(occ.contract_type) if occ is not None
                    else contract_type_of(str(stored_proposal.get("contractType", "call")))
                ),
                strike=(
                    occ.strike if occ is not None
                    else float(stored_proposal.get("strike", 0.0) or 0.0)
                ),
                expiry=occ.expiry if occ is not None else date.today(),
                multiplier=multiplier,
                action="sell_to_close",
            )
            risk_proposal: RiskProposal = to_risk_proposal(
                symbol=wire_symbol,
                side=RiskSide.SELL,
                qty=qty,
                estimated_notional=round(qty * last_price * multiplier, 2),
                last_price=last_price,
                confidence=1.0,  # exits aren't conviction-gated
                option=option,
            )
        else:
            is_short = held is not None and held.qty < 0
            close_side = RiskSide.BUY if is_short else RiskSide.SELL
            broker_close_side = Side.BUY if is_short else Side.SELL
            db_side = broker_close_side.value
            order_type = OrderType.MARKET
            last_price = (
                held.market_value / held.qty
                if held is not None
                else (float(decision.fill_avg_price or 0) or 1.0)
            )
            risk_proposal = RiskProposal(
                symbol=symbol,
                side=close_side,
                qty=qty,
                estimated_notional=round(qty * last_price, 2),
                last_price=last_price,
                confidence=1.0,  # exits aren't conviction-gated
            )

        risk_decision = evaluate(risk_proposal, risk_ctx, None)
        if not risk_decision.approved:
            logger.warning(
                "position_manager: close VETOED for %s — %s (%s)",
                symbol, risk_decision.veto_rule, risk_decision.reason,
            )
            return False

        canceled = await broker.cancel_open_orders(wire_symbol)
        if canceled:
            logger.info(
                "position_manager: canceled %d resting orders on %s before close",
                canceled, symbol,
            )

        order_row_id = await persist_linked_order_submit(
            user_id=user_id,
            broker_connection_id=conn.id,
            decision_id=decision.id,
            client_order_id=client_order_id,
            symbol=wire_symbol,
            side=db_side,
            qty=qty,
            is_paper=conn.is_paper,
            order_type=order_type.value,
            is_option=is_option,
            multiplier=multiplier,
            option_action="sell_to_close" if is_option else None,
        )

        order = await broker.place_order(
            OrderRequest(
                symbol=wire_symbol,
                side=broker_close_side,
                qty=qty,
                order_type=order_type,
                limit_price=round(last_price, 2) if order_type is OrderType.LIMIT else None,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
        )

        if order_row_id is not None:
            try:
                await persist_order_result(order_row_id=order_row_id, broker_order=order)
            except Exception:
                logger.exception("position_manager: persist_order_result failed")

    # Stamp WHY now; closed_at + realized P&L land when the fill confirms.
    from engine.db.models import AgentDecision

    async with session_factory() as session:
        await session.execute(
            update(AgentDecision)
            .where(AgentDecision.id == decision.id)
            .values(close_reason=reason)
        )
        await session.commit()

    label = _CLOSE_REASON_LABEL.get(reason, reason)
    logger.info(
        "position_manager: closing %d %s for user=%s — %s (broker_order=%s)",
        qty, symbol, user_id, label, order.broker_order_id,
    )
    _notify_close(
        user_id=user_id, symbol=symbol, qty=qty, label=label, side=broker_close_side.value
    )
    return True


def _notify_close(*, user_id: str, symbol: str, qty: int, label: str, side: str = "SELL") -> None:
    try:
        from app.services.notifications.notifications import schedule_position_event_notification

        schedule_position_event_notification(
            user_id=user_id,
            title="Agent closing position",
            body=f"{side} {qty} {symbol} — {label}. Tap for the trade log.",
        )
    except Exception:
        logger.exception("position_manager: close notification failed")
