"""The escalation loop — docs/IMPL_OPTIONS_AGENTS.md §5 / docs/PLAN_OPTIONS_AGENTS.md §5.

WHAT THIS IS. The deterministic trailing ratchet
(``engine.options.exits.option_ratchet_signal``, driven every 30s by
``app.services.orders.position_manager.manage_positions_for_user``) is the
PRIMARY safety net for every open option position and it never waits for a
model. This module adds a SECOND, LATER, and only ever MORE conservative
check: when the ratchet reports a material change on a position it chose to
keep holding, a model gets one guarded chance to bank a gain, tighten
protection, or close early — never to loosen anything, and never to place
an order faster than the deterministic checks in ``tools/guard.py`` allow.

THE DESIGN DECISION THIS MODULE MAKES: ONE AGENT, NOT TWO.

``options/agents.py``'s OPENING flow uses two independent agents (Bull,
Bear) precisely because its whole point is to prevent one enthusiastic take
from committing FRESH risk budget before any capital is at risk — agreement
is required, and the size is set by whichever agent is LESS confident. That
property is worth two LLM calls because the alternative (a single agent's
say-so opening a position) has no independent check on it at all.

Escalation is a different problem. The position already exists, already
passed the entry approval, and the deterministic ratchet is ALREADY running
on it every tick regardless of what any model says (docs/OPTIONS_PLAYBOOK.md
§3: "this is the safety net and it never waits for a model"). Every action
available here is independently bounded by ``tools/guard.py``'s ratchet
invariant no matter how many models recommend it, or how confidently:
  - EXIT_NOW is unconditionally allowed — de-risking is never blocked, so
    there is no "second opinion" that could usefully veto it.
  - TIGHTEN_STOP / RAISE_TAKE_PROFIT only accept a STRICTLY tighter/higher
    value than the position already has — a value that would loosen
    protection is refused mechanically, regardless of how the request is
    argued.
  - SCALE_IN re-runs the FULL risk engine and is hard-capped at 2 adds —
    conviction cannot buy its way past either limit.
There is no "unsafe direction" for a second, adversarial agent to catch
here that the guard does not already catch deterministically — unlike the
OPENING flow, where two-agent disagreement is the ONLY thing standing
between a lone take and a live order. Running a Bull-of-the-position and a
Bear-of-the-position here would double the latency and the cost of every
escalation for no matching safety property, and would actively work
against the "1 escalation per fleet tick" budget this module also enforces
(docs §5.1) — that budget exists specifically to bound the 30s tick's
latency across the WHOLE fleet, not just one position.

A single "Options Escalation Agent" role (``prompts.OPTIONS_ESCALATION``) is
also the right IDENTITY, not a re-use of Bull or Bear: the job here is
neither "argue for the trade" (Bull already won that argument, at entry)
nor "argue against it" (Bear's adversarial skepticism belongs at entry,
not on an already-approved position) — it is a sober portfolio-management
review of an existing position against its own original thesis and
deadline. Reusing Bull's persona would bias it toward staying attached to a
position that has stopped working; reusing Bear's would bias toward
premature de-risking on exactly the ticks (like "just armed") where the
correct answer is often to do nothing. This also matches this repo's own
naming precedent: CLAUDE.md's plan table describes the adjacent, not-yet-
built follow-on work as "a monotone LLM exit agent" — singular — not a
second arguing pair.

FAIL-SAFE (docs §5.3): an error, a timeout, malformed output, or MOCK mode
must never move a position. ``run_escalation`` below catches every
exception around the one LLM round trip and returns an EMPTY tool
transcript on failure — meaning ``tools/guard.py`` was never even asked to
approve anything, so the position's stored stop/take-profit/adds state is
byte-identical to before, and no order was placed. MOCK mode reaches the
SAME empty-transcript outcome through the ordinary path, not a special
case: ``llm.py``'s mock responses never emit a ``tool_use`` block for ANY
role (see ``llm.py::complete_tools``'s own "MOCK is TEXT ONLY" docstring),
so ``run_tool_loop`` returns with zero tool calls and this module never
reaches ``dispatch_tool_call`` at all.

A FLAGGED GAP IN ALREADY-SHIPPED CODE, DEFENDED HERE RATHER THAN FIXED
UPSTREAM. ``tools/guard.py::_before_open_option_trade`` checks
``AUTO_TRADE_ENABLED``, paper-only, and market-open as its first three
steps. ``_before_adjust_option_position`` — already landed, already tested,
out of this module's scope to rebuild — checks NONE of these before
allowing EXIT_NOW/SCALE_IN to place a real broker order, which contradicts
docs/PLAN_OPTIONS_AGENTS.md §4's own words ("Hard-coded, not configurable:
paper only... checked in `before`, regardless of any flag"). This module is
the FIRST thing that calls ``adjust_option_position`` from a live,
unattended, scheduled path (the 30s fleet tick) rather than from a test or
an LLM hop nothing in production invokes yet, so ``_rate_limit_reason`` and
``run_escalation``'s own dispatch wrapper both re-assert that missing gate
here, defensively, rather than silently relying on a check that is not
actually there yet. See this session's build-log entry / final report for
the flagged follow-up to close the gap in ``guard.py`` itself.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

from engine.env import env_flag
from engine.features.market_calendar import is_us_market_open
from engine.options.exits import RatchetOutcome
from engine.risk import RiskCaps
from trading_agents.llm import LLM, Model
from trading_agents.llm_loop import run_tool_loop
from trading_agents.options.prompts import OPTIONS_ESCALATION
from trading_agents.options.tools.guard import (
    GuardContext,
    ToolGuard,
    _is_paper_and_safe,
    dispatch_tool_call,
    persist_option_state,
)
from trading_agents.options.tools.readonly import _parse_thesis_deadline
from trading_agents.options.tools.registry import REGISTRY
from trading_agents.options.tools.schemas import ADJUST_OPTION_POSITION, READ_ONLY_TOOLS

logger = logging.getLogger("agents.options.escalation")

__all__ = [
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_MAX_PER_DAY",
    "MAX_ESCALATIONS_PER_FLEET_TICK",
    "EscalationBudget",
    "EscalationOutcome",
    "EscalationTrigger",
    "PositionBrief",
    "escalation_env_config",
    "evaluate_escalation_trigger",
    "load_escalation_state",
    "maybe_escalate",
    "run_escalation",
]

# Mirrors options/agents.py's own DEFAULT_MAX_ROUNDS — one propose round
# plus room for a denial-and-retry, same tool-loop cap the opening flow
# uses (docs/IMPL_OPTIONS_AGENTS.md §6 OPTIONS_AGENT_MAX_ROUNDS).
_ESCALATION_MAX_ROUNDS = 3
_ESCALATION_MAX_TOKENS = 700

DEFAULT_COOLDOWN_S = 900.0
DEFAULT_MAX_PER_DAY = 4

# Hardcoded, NOT env-tunable: this bounds the WHOLE fleet tick's latency
# across every user and every position being managed that tick (docs
# §5.1: "1 escalation per fleet tick... across ALL positions, not
# per-position") — a structural latency budget, not a per-position risk
# parameter a deploy might reasonably want to widen. Mirrors
# tools/guard.py's own `_MAX_SCALE_IN_ADDS` in spirit: a safety constant
# lives in code, not in an env var.
MAX_ESCALATIONS_PER_FLEET_TICK = 1

# docs/IMPL_OPTIONS_AGENTS.md §5.1's four material-change triggers.
_PEAK_ADVANCE_TRIGGER_PP = 15.0
_NEAR_TRAIL_LINE_PP = 10.0
_DTE_LOW_THRESHOLD = 5

_ESCALATION_TOOLS: tuple[dict[str, Any], ...] = (ADJUST_OPTION_POSITION, *READ_ONLY_TOOLS)


# ─────────────────────────────────────────────────────────────────────
# Env config — same fail-to-default contract as engine.risk.types'
# _env_float/_env_int (a malformed value keeps the default and logs,
# never silently disables or widens a limit).
# ─────────────────────────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring malformed %s=%r — keeping %r", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("ignoring malformed %s=%r — keeping %r", name, raw, default)
        return default


@dataclass(frozen=True)
class EscalationEnvConfig:
    cooldown_s: float
    max_per_day: int


def escalation_env_config() -> EscalationEnvConfig:
    """docs/IMPL_OPTIONS_AGENTS.md §6: ``OPTIONS_ESCALATION_COOLDOWN_S`` /
    ``OPTIONS_ESCALATION_MAX_PER_DAY``."""
    return EscalationEnvConfig(
        cooldown_s=_env_float("OPTIONS_ESCALATION_COOLDOWN_S", DEFAULT_COOLDOWN_S),
        max_per_day=_env_int("OPTIONS_ESCALATION_MAX_PER_DAY", DEFAULT_MAX_PER_DAY),
    )


# ─────────────────────────────────────────────────────────────────────
# Fleet-tick budget — mutable, shared-by-reference, cross-call state the
# CALLER owns. Deliberately NOT a parameter of the pure trigger evaluator
# below: a single instance must be constructed ONCE per
# ``ReconcilerFleet.tick()`` and threaded into every
# ``manage_positions_for_user`` call within that SAME tick (see
# ``reconciler_fleet.py``), so it cannot be re-derived per-position the
# way every other input to ``evaluate_escalation_trigger`` can.
# ─────────────────────────────────────────────────────────────────────


class EscalationBudget:
    """Bounds how many escalation LLM calls may fire across one fleet
    tick, system-wide.

    Plain synchronous check-and-decrement — safe under asyncio's
    single-threaded cooperative scheduling because ``try_consume`` never
    awaits between checking and decrementing, so no other coroutine can
    interleave a use of the same instance in between.
    """

    def __init__(self, limit: int = MAX_ESCALATIONS_PER_FLEET_TICK) -> None:
        self._remaining = limit

    def try_consume(self) -> bool:
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True

    @property
    def remaining(self) -> int:
        return self._remaining


# ─────────────────────────────────────────────────────────────────────
# The pure trigger + rate-limit evaluator
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EscalationTrigger:
    """The deterministic verdict on whether THIS tick should escalate for
    one position. Pure given its inputs — no DB, no LLM, no fleet-tick
    budget (that one is checked by the caller, one layer up — see
    ``evaluate_escalation_trigger``'s docstring)."""

    should_escalate: bool
    material_change: str | None
    """Named, like a risk veto rule: ``ratchet_armed`` | ``peak_advanced``
    | ``near_trail_line`` | ``dte_low`` | ``None`` (no material change
    this tick)."""
    reason: str | None
    """Why ``should_escalate`` is False: ``no_material_change`` |
    ``auto_trade_disabled`` | ``live_mode_refused`` | ``market_closed`` |
    ``cooldown_active`` | ``daily_cap_reached`` | ``None`` when
    ``should_escalate`` is True. ``fleet_tick_budget_exhausted`` is a
    valid value too, but only ever set by ``maybe_escalate`` AFTER this
    function already returned True — see its docstring for why that gate
    is not part of this dataclass's own construction."""


def _detect_material_change(
    *,
    ratchet_outcome: RatchetOutcome,
    was_armed_before: bool,
    dte: int | None,
    last_escalated_peak_pct: float | None,
) -> str | None:
    """docs/IMPL_OPTIONS_AGENTS.md §5.1's four triggers, checked in the
    order the spec lists them — the first one that fires names the
    trigger, mirroring the "first-veto-wins" / "first funnel stage that
    empties names the rejection" convention already established
    throughout this codebase (``engine.options.selection``,
    ``engine.risk``).

    Only ever called from a HOLD ratchet outcome — the caller only
    invokes this once ``_exit_reason`` has already returned ``None`` for
    this tick — so ``ratchet_outcome.pnl_pct`` is either ``None`` (no
    broker mark this tick) or, when armed, strictly above the trail line
    (Rule 3 of ``option_ratchet_signal`` would otherwise already have
    fired a CLOSE this same tick).
    """
    if ratchet_outcome.armed and not was_armed_before:
        return "ratchet_armed"

    # No escalation has ever fired for this position: treat the baseline
    # as 0.0 rather than skipping the check entirely. In the overwhelming
    # common case this is moot (a position's FIRST-ever escalation is
    # almost always "ratchet_armed", checked above, since peak crossing
    # the arm threshold for the first time both arms the ratchet AND
    # necessarily advances the peak) — this baseline only matters for a
    # position whose escalation history predates this feature, or one
    # that was already armed before its first material-change check for
    # some other reason. Treating "never escalated" as "advanced from
    # zero" is the conservative direction: it can only make an old
    # position MORE eligible to be looked at, never less.
    baseline_peak = last_escalated_peak_pct if last_escalated_peak_pct is not None else 0.0
    if ratchet_outcome.peak_pl_pct - baseline_peak >= _PEAK_ADVANCE_TRIGGER_PP:
        return "peak_advanced"

    if (
        ratchet_outcome.armed
        and ratchet_outcome.trail_line_pct is not None
        and ratchet_outcome.pnl_pct is not None
        and (ratchet_outcome.pnl_pct - ratchet_outcome.trail_line_pct) <= _NEAR_TRAIL_LINE_PP
    ):
        return "near_trail_line"

    if dte is not None and dte <= _DTE_LOW_THRESHOLD:
        return "dte_low"

    return None


def _mutation_gate_reason(*, now: datetime) -> str | None:
    """Re-derives ``tools/guard.py::_before_open_option_trade``'s steps
    1-3 (``AUTO_TRADE_ENABLED``, paper-only, market-open) for the ONE
    mutating tool this module's dispatch closure can reach. See this
    module's own docstring ("A FLAGGED GAP...") for why this duplication
    is deliberate rather than redundant: ``_before_adjust_option_position``
    does not (yet) check any of these itself. Reuses ``tools/guard.py``'s
    OWN ``_is_paper_and_safe`` rather than a third reimplementation of
    that exact check (CLAUDE.md §4.4 — the same threshold/check
    duplicated across files is a bug waiting to drift)."""
    if not env_flag("AUTO_TRADE_ENABLED"):
        return "auto_trade_disabled"
    if not _is_paper_and_safe():
        return "live_mode_refused"
    if not is_us_market_open(now):
        return "market_closed"
    return None


def _rate_limit_reason(
    *,
    now: datetime,
    last_escalation_at: datetime | None,
    escalations_today: int,
    cooldown_s: float,
    max_per_day: int,
) -> str | None:
    """Gates independent of WHETHER a material change fired — only ever
    consulted once a material change actually exists, so a quiet position
    never reaches this at all."""
    gate = _mutation_gate_reason(now=now)
    if gate is not None:
        return gate
    if last_escalation_at is not None:
        elapsed = (now - last_escalation_at).total_seconds()
        if elapsed < cooldown_s:
            return "cooldown_active"
    if escalations_today >= max_per_day:
        return "daily_cap_reached"
    return None


def evaluate_escalation_trigger(
    *,
    ratchet_outcome: RatchetOutcome,
    was_armed_before: bool,
    dte: int | None,
    last_escalation_at: datetime | None,
    escalations_today: int,
    last_escalated_peak_pct: float | None,
    now: datetime,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    max_per_day: int = DEFAULT_MAX_PER_DAY,
) -> EscalationTrigger:
    """Combines the material-change detector with the rate-limit gates.

    Deliberately does NOT take the fleet-tick budget as a parameter: that
    budget is mutable, shared, cross-call state (``EscalationBudget``)
    that must be consumed atomically by the CALLER only for a candidate
    that has already cleared every gate here — checking it inside this
    function (or before calling it) would risk spending the fleet's one
    shared slot on a position that was going to be cooldown- or
    cap-blocked anyway. See ``maybe_escalate`` for where that final gate
    lives.
    """
    material_change = _detect_material_change(
        ratchet_outcome=ratchet_outcome,
        was_armed_before=was_armed_before,
        dte=dte,
        last_escalated_peak_pct=last_escalated_peak_pct,
    )
    if material_change is None:
        return EscalationTrigger(False, None, "no_material_change")

    gate_reason = _rate_limit_reason(
        now=now,
        last_escalation_at=last_escalation_at,
        escalations_today=escalations_today,
        cooldown_s=cooldown_s,
        max_per_day=max_per_day,
    )
    if gate_reason is not None:
        return EscalationTrigger(False, material_change, gate_reason)

    return EscalationTrigger(True, material_change, None)


# ─────────────────────────────────────────────────────────────────────
# Escalation state — new keys living alongside the ratchet's own
# (peak_pl_pct/armed/trail_line_pct) and the guard's own
# (stop_loss_pct/take_profit_pct/adds_this_position) inside the SAME
# ``reasoning.option_exit`` JSONB blob (CLAUDE.md's own established
# convention: extend the existing key, never a second one, never a
# whole-column overwrite).
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EscalationState:
    last_escalation_at: datetime | None
    escalations_today: int
    escalations_date: date | None
    last_escalated_peak_pct: float | None
    was_armed: bool


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _parse_iso_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def load_escalation_state(option_exit: dict[str, Any]) -> EscalationState:
    """Reads the escalation-tracking fields this module owns out of the
    ``option_exit`` dict already loaded from ``decision.reasoning`` —
    never a second DB read just to see these. Malformed/missing values
    degrade to "never escalated" rather than raising, matching every
    other reader of this JSONB blob in this codebase."""
    last_escalation_at = _parse_iso_datetime(option_exit.get("last_escalation_at"))
    escalations_date = _parse_iso_date(option_exit.get("escalations_date"))
    try:
        escalations_today = int(option_exit.get("escalations_today", 0) or 0)
    except (TypeError, ValueError):
        escalations_today = 0
    raw_peak = option_exit.get("last_escalated_peak_pct")
    try:
        last_escalated_peak_pct = float(raw_peak) if raw_peak is not None else None
    except (TypeError, ValueError):
        last_escalated_peak_pct = None
    return EscalationState(
        last_escalation_at=last_escalation_at,
        escalations_today=escalations_today,
        escalations_date=escalations_date,
        last_escalated_peak_pct=last_escalated_peak_pct,
        was_armed=bool(option_exit.get("armed", False)),
    )


def _escalations_today_for(state: EscalationState, *, today: date) -> int:
    """A calendar-day rollover (UTC, matching every other date comparison
    in ``position_manager.py`` — e.g. its own ``held_days`` time-stop
    math) resets the daily counter to 0."""
    if state.escalations_date != today:
        return 0
    return state.escalations_today


async def _persist_escalation_attempt(
    session_factory: Any,
    *,
    decision_id: str,
    last_escalation_at: datetime,
    escalations_today: int,
    escalations_date: date,
    last_escalated_peak_pct: float,
) -> None:
    """Re-reads the CURRENT ``option_exit`` state immediately before
    writing, rather than merging over a stale in-memory snapshot.

    This matters because this write is NOT the only thing that may have
    touched ``option_exit`` since the caller's snapshot was taken: the
    escalation's own tool call, if it succeeded, may have JUST updated
    ``stop_loss_pct``/``take_profit_pct``/``adds_this_position`` (via
    ``tools/guard.py``'s ``persist_option_state``, called from inside the
    tool dispatch this function's caller already awaited), and the SAME
    tick's ratchet peak-write may ALSO have landed. Merging from a
    snapshot captured before either of those writes would silently revert
    them — exactly the class of bug the ratchet invariant exists to
    prevent. This function's own updates therefore touch ONLY the four
    escalation-bookkeeping keys below, never any key another code path
    owns.
    """
    if session_factory is None:
        return
    try:
        did = uuid.UUID(str(decision_id))
    except (ValueError, TypeError):
        return

    from engine.db.models import AgentDecision

    async with session_factory() as session:
        row = await session.get(AgentDecision, did)
    current = dict((getattr(row, "reasoning", None) or {}).get("option_exit") or {})
    merged = {
        **current,
        "last_escalation_at": last_escalation_at.isoformat(),
        "escalations_today": escalations_today,
        "escalations_date": escalations_date.isoformat(),
        "last_escalated_peak_pct": last_escalated_peak_pct,
    }
    await persist_option_state(session_factory, decision_id=decision_id, state=merged)


# ─────────────────────────────────────────────────────────────────────
# The brief — "what the agents receive" (docs §5.2)
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PositionBrief:
    """Everything docs/IMPL_OPTIONS_AGENTS.md §5.2 says the agent
    receives, assembled by the caller from data it already has in hand
    this tick (the already-loaded decision row + the already-computed
    ``RatchetOutcome``) — no extra DB round trip needed to build this,
    matching ``options/agents.py``'s own "COMPLETE deterministic
    pre-pass... you do not need to fetch anything in the common case"
    philosophy for the opening flow's Bull/Bear prompts."""

    decision_id: str
    underlying: str
    entry_premium: float | None
    current_pl_pct: float | None
    peak_pl_pct: float
    trail_line_pct: float | None
    armed: bool
    dte: int | None
    days_held: int | None
    thesis: str
    deadline: date | None
    deadline_passed: bool
    trigger: str


def build_position_brief(
    decision: Any,
    *,
    ratchet_outcome: RatchetOutcome,
    dte: int | None,
    trigger: str,
    now: datetime,
) -> PositionBrief:
    """Pure given its inputs (no I/O) — the SAME field-reading convention
    ``tools/readonly.py``'s ``get_position_snapshot``/``get_entry_thesis``
    already use (entry premium from ``fill_avg_price`` falling back to the
    stored limit price; thesis from ``proposal.rationale`` falling back to
    ``reasoning.drafter_rationale`` — see that module's own docstring for
    why THAT specific fallback order is correct for an approved/filled
    row). Duplicated here in a few lines rather than calling those
    handlers directly, deliberately: they each do their OWN DB fetch by
    ``decision_id``, and this caller already has the row loaded — a
    second fetch of data already in hand would cost a DB round trip on
    every escalation for no benefit.
    """
    proposal = decision.proposal or {}
    reasoning = decision.reasoning or {}

    raw_limit_price = proposal.get("limitPrice", proposal.get("limit_price"))
    entry_premium = (
        float(decision.fill_avg_price)
        if decision.fill_avg_price is not None
        else (float(raw_limit_price) if raw_limit_price is not None else None)
    )

    entered_at = decision.user_responded_at or decision.triggered_at
    days_held = (now.date() - entered_at.date()).days if entered_at is not None else None

    thesis = str(proposal.get("rationale") or reasoning.get("drafter_rationale") or "").strip()
    anchor = (entered_at or now).date()
    deadline = _parse_thesis_deadline(thesis, anchor=anchor)

    return PositionBrief(
        decision_id=str(decision.id),
        underlying=decision.symbol,
        entry_premium=entry_premium,
        current_pl_pct=ratchet_outcome.pnl_pct,
        peak_pl_pct=ratchet_outcome.peak_pl_pct,
        trail_line_pct=ratchet_outcome.trail_line_pct,
        armed=ratchet_outcome.armed,
        dte=dte,
        days_held=days_held,
        thesis=thesis,
        deadline=deadline,
        deadline_passed=bool(deadline is not None and now.date() > deadline),
        trigger=trigger,
    )


def _render_escalation_brief(brief: PositionBrief) -> str:
    lines = [
        f"decision_id: {brief.decision_id}",
        f"underlying: {brief.underlying}",
        f"material_change_trigger: {brief.trigger}",
        "",
        "Position state (from the deterministic trailing ratchet, already "
        "computed — you do not need to fetch this):",
        f"  entry_premium: {brief.entry_premium if brief.entry_premium is not None else 'n/a'}",
        "  current_pl_pct: "
        + (f"{brief.current_pl_pct:.1f}" if brief.current_pl_pct is not None else "n/a (no broker mark this tick)"),
        f"  peak_pl_pct: {brief.peak_pl_pct:.1f}",
        "  trail_line_pct: "
        + (f"{brief.trail_line_pct:.1f}" if brief.trail_line_pct is not None else "not yet armed"),
        f"  armed: {brief.armed}",
        f"  dte: {brief.dte if brief.dte is not None else 'n/a'}",
        f"  days_held: {brief.days_held if brief.days_held is not None else 'n/a'}",
        "",
        f"Original thesis: {brief.thesis or 'n/a'}",
        f"Parsed deadline: {brief.deadline.isoformat() if brief.deadline else 'n/a (no parseable timeframe)'}",
        f"Deadline passed: {brief.deadline_passed}",
        "",
        f"Call adjust_option_position with decision_id={brief.decision_id!r} "
        "if you want to act. HOLD (or simply not calling the tool) is a "
        "valid, common answer.",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# The one LLM hop
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EscalationOutcome:
    trigger: str
    errored: bool
    """True iff the LLM round trip itself failed (network error, timeout,
    an unexpected bug) — in which case ``tool_transcript`` is guaranteed
    empty: no tool was ever dispatched, so nothing about the position's
    stored protection state changed (docs §5.3's fail-safe)."""
    tool_transcript: tuple[dict[str, Any], ...]
    final_text: str | None


async def run_escalation(
    *,
    brief: PositionBrief,
    user_id: str,
    now: datetime,
    llm: LLM,
    guard: ToolGuard,
    caps: RiskCaps,
    max_rounds: int = _ESCALATION_MAX_ROUNDS,
) -> EscalationOutcome:
    """The one LLM hop for an escalation: a SINGLE agent (see this
    module's docstring for why one, not two) reviews an already-open,
    already-approved position and may call ``adjust_option_position``
    once.

    Mirrors ``options/agents.py::run_options_agents``'s own
    ``_dispatch`` closure pattern: a fresh ``GuardContext`` per attempt
    (here there is only ever one attempt, since ``calls_this_pass``/
    ``resolved_direction``/``resolved_conviction`` do not apply to
    ``adjust_option_position`` at all — see ``GuardContext``'s own
    docstring), wrapping ``dispatch_tool_call`` rather than calling it
    directly so a defensive gate can run first for the one mutating tool
    this hop can reach (see this module's docstring, "A FLAGGED GAP...").

    FAIL-SAFE: any exception raised while running the tool loop — a
    network error, a timeout, a bug — is caught here and reported as
    ``errored=True`` with an EMPTY transcript. Nothing about the
    position's protection state can have changed, because
    ``dispatch_tool_call`` (and therefore ``tools/guard.py``) was never
    reached.
    """
    ctx = GuardContext(
        user_id=user_id,
        council_run_id=brief.decision_id,
        # Not applicable to adjust_option_position — only
        # open_option_trade's `before()` reads these two fields.
        resolved_direction=None,
        resolved_conviction=None,
        calls_this_pass=0,
        caps=caps,
    )

    async def _dispatch(call: Any) -> dict[str, Any]:
        if call.name == "adjust_option_position":
            gate = _mutation_gate_reason(now=now)
            if gate is not None:
                return {"is_error": True, "content": {"denied": gate}}
        return await dispatch_tool_call(call, ctx, guard=guard, registry=REGISTRY)

    try:
        resp, transcript = await run_tool_loop(
            llm,
            system=OPTIONS_ESCALATION,
            user=_render_escalation_brief(brief),
            tools=list(_ESCALATION_TOOLS),
            dispatch=_dispatch,
            model=Model.SONNET,
            max_rounds=max_rounds,
            max_tokens=_ESCALATION_MAX_TOKENS,
            council_run_id=brief.decision_id,
            user_id=user_id,
        )
    except Exception:
        logger.exception(
            "escalation: run_tool_loop raised for decision=%s trigger=%s — "
            "treating as a no-op; the deterministic ratchet keeps running "
            "untouched",
            brief.decision_id, brief.trigger,
        )
        return EscalationOutcome(brief.trigger, True, (), None)

    return EscalationOutcome(brief.trigger, False, tuple(transcript), resp.text)


# ─────────────────────────────────────────────────────────────────────
# The entry point position_manager.py calls
# ─────────────────────────────────────────────────────────────────────


async def maybe_escalate(
    *,
    decision: Any,
    ratchet_outcome: RatchetOutcome,
    dte: int | None,
    now: datetime,
    budget: EscalationBudget,
    llm: LLM,
    guard: ToolGuard,
    caps: RiskCaps,
    session_factory: Any,
    cooldown_s: float | None = None,
    max_per_day: int | None = None,
) -> EscalationTrigger:
    """One open, ratchet-managed option position, one tick: evaluate the
    trigger, and — only when it fires and the fleet-tick budget allows —
    run the one LLM hop and persist the escalation bookkeeping.

    ``cooldown_s``/``max_per_day`` default to ``None``, meaning "read
    ``OPTIONS_ESCALATION_COOLDOWN_S``/``OPTIONS_ESCALATION_MAX_PER_DAY``
    from the environment via ``escalation_env_config()``" — NOT a literal
    default baked into the signature. A literal default here would mean
    those two documented env vars (docs/IMPL_OPTIONS_AGENTS.md §6) never
    actually applied in the real call path, since
    ``position_manager.py``'s wrapper never overrides them either; tests
    that want a specific value still pass one explicitly.

    Returns the ``EscalationTrigger`` either way (for logging); the
    caller does not need to inspect it further. Never raises: a failure
    persisting the bookkeeping is caught and logged, matching
    ``manage_positions_for_user``'s own established "continuing without
    it" convention right next to where this is called from.
    """
    if cooldown_s is None or max_per_day is None:
        env_config = escalation_env_config()
        cooldown_s = env_config.cooldown_s if cooldown_s is None else cooldown_s
        max_per_day = env_config.max_per_day if max_per_day is None else max_per_day

    reasoning = decision.reasoning or {}
    option_exit = dict(reasoning.get("option_exit") or {})
    state = load_escalation_state(option_exit)
    escalations_today = _escalations_today_for(state, today=now.date())

    trigger = evaluate_escalation_trigger(
        ratchet_outcome=ratchet_outcome,
        was_armed_before=state.was_armed,
        dte=dte,
        last_escalation_at=state.last_escalation_at,
        escalations_today=escalations_today,
        last_escalated_peak_pct=state.last_escalated_peak_pct,
        now=now,
        cooldown_s=cooldown_s,
        max_per_day=max_per_day,
    )
    if not trigger.should_escalate:
        return trigger

    if not budget.try_consume():
        logger.info(
            "escalation: material change (%s) for %s (%s) but this "
            "fleet tick's escalation budget is already spent",
            trigger.material_change, decision.symbol, decision.id,
        )
        return replace(trigger, should_escalate=False, reason="fleet_tick_budget_exhausted")

    brief = build_position_brief(
        decision, ratchet_outcome=ratchet_outcome, dte=dte,
        trigger=trigger.material_change or "unknown", now=now,
    )
    outcome = await run_escalation(
        brief=brief, user_id=str(decision.user_id), now=now, llm=llm, guard=guard, caps=caps,
    )
    logger.info(
        "escalation: attempted for %s (%s) trigger=%s errored=%s tool_calls=%d",
        decision.symbol, decision.id, trigger.material_change,
        outcome.errored, len(outcome.tool_transcript),
    )

    try:
        await _persist_escalation_attempt(
            session_factory,
            decision_id=str(decision.id),
            last_escalation_at=now,
            escalations_today=escalations_today + 1,
            escalations_date=now.date(),
            last_escalated_peak_pct=ratchet_outcome.peak_pl_pct,
        )
    except Exception:
        logger.exception(
            "escalation: failed to persist escalation bookkeeping for %s "
            "(%s) — continuing without it",
            decision.symbol, decision.id,
        )

    return trigger
