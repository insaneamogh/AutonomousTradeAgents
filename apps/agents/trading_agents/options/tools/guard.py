"""ToolGuard — the deterministic gate between an options agent's tool
calls and ``packages/broker``.

Per ``docs/IMPL_OPTIONS_AGENTS.md`` §2: the agent never supplies a
contract, strike, expiry or quantity, and it never gets a straight line to
the broker. ``before()`` runs the full 12-step stack for
``open_option_trade`` (culminating in the SAME ``engine.risk.evaluate()``
every equity/options proposal already runs through — this file does not
reimplement any risk logic, it only decides WHEN to call it) and the
ratchet invariant for ``adjust_option_position``. ``after()`` audits every
call and narrows-never-widens the result. Only a handler in ``trade.py``,
invoked after ``before()`` allows, ever reaches ``packages/broker``.

Dependency injection: ``ToolGuard`` resolves its own infrastructure
(risk-context provider, decision log, DB session factory, broker factory)
lazily, mirroring ``trading_agents.nodes.risk_officer._default_provider``'s
``USE_POSTGRES`` switch — but every one of them is constructor-overridable
so tests never need a live database or a live Alpaca connection (this repo
has no live-DB test harness at all; see ``position_manager.py``'s
``_option_exit_peak_update_stmt`` docstring for the same convention).

``guard.py`` resolves ALL of that infrastructure ONCE per call and hands
the resolved instances down through ``GuardVerdict.payload`` — the
IMPL doc's ``dispatch()`` pseudocode calls a handler as ``handler(input,
ctx)``; this file's ``dispatch_tool_call`` calls it as ``handler(input,
ctx, guard_payload)``. That third argument is this module's own addition
(nothing here is a frozen external contract except the tool schemas,
``GuardVerdict``/``GuardContext``, and the denial shape) and it is what
keeps ``trade.py`` a thin "place the order, write the row" executor with
no risk logic of its own left to get wrong.

🚨 Sign convention for ``adjust_option_position``'s ratchet invariant —
READ BEFORE CHANGING ANYTHING IN §"ratchet invariant" BELOW:

``docs/IMPL_OPTIONS_AGENTS.md`` §2.2 and ``docs/PLAN_OPTIONS_AGENTS.md``
§3.3 both describe ``TIGHTEN_STOP`` as "value must INCREASE" / "may only
move UP (tighter)". Taken completely literally for ``stop_loss_pct``,
that is backwards: this repo's OWN established convention for that exact
field name (``RiskCaps.options_stop_loss_pct`` — "positive magnitude,
50.0 means down 50%") makes a SMALLER number the tighter stop, and
``docs/OPTIONS_PLAYBOOK.md`` §2 documents the aggressive-paper profile
moving it 50.0 -> 40.0 UNDER THE EXPLICIT LABEL "cut losers early" — a
DECREASE described, in this very codebase, as tightening. Given CLAUDE.md
§4.2's instruction to trust the code (and this repo's own already-shipped
precedent) over a plan doc's prose when they conflict, and given how
safety-critical getting this backwards would be (silently ALLOWING loss
widening under the label of "tightening"), this file implements
``TIGHTEN_STOP`` as "the new stop_loss_pct must be STRICTLY SMALLER than
the current one" — the evidence-backed direction, not the plan docs'
literal "must increase" wording. ``RAISE_TAKE_PROFIT`` is implemented per
the docs' literal wording ("must increase") because that field's
direction is safety-NEUTRAL either way (it only ever gates how a WINNING
position banks profit; it can never widen how much the position can
lose), so there is no comparable reason to override the literal spec
there. FLAG THIS to whoever owns ``options/agents.py`` / ``prompts.py``
(a separate workstream from this file) — the Bull/Bear prompts must tell
the model the SAME direction this guard enforces, or every
``TIGHTEN_STOP`` call the model makes will be denied as
``cannot_loosen_protection``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from engine.env import env_flag
from engine.features.market_calendar import is_us_market_open
from engine.options import (
    ContractSelectionInputs,
    OptionsSizingInputs,
    fetch_option_candidates,
    options_position_size,
    select_contract,
    to_risk_proposal,
)
from engine.risk import (
    MockRiskContextProvider,
    PostgresRiskContextProvider,
    RiskCaps,
    RiskContextProvider,
    Side,
    evaluate,
)
from engine.risk.types import OptionLegDetails
from trading_agents.options.tools.schemas import READ_ONLY_TOOLS
from trading_agents.strategies import STRATEGY_REGISTRY

logger = logging.getLogger("agents.options.guard")

__all__ = [
    "SYMBOL_RE",
    "GuardContext",
    "GuardVerdict",
    "ToolGuard",
    "dispatch_tool_call",
    "persist_option_state",
    "stamp_position_closed",
]

# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# Plain US equity ticker: 1-6 uppercase letters, optional ".A"/".B" share
# class suffix (e.g. BRK.B). The agent supplies the underlying, never an
# OCC symbol — this rejects anything that even LOOKS like one (OCC symbols
# are 15+ chars ending in digits) as a cheap, early, named refusal.
SYMBOL_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$")

# A thesis must name a timeframe (IMPL doc §2.1 step 8) — theta is always
# against a long option, so an undated thesis can never be checked. This
# is deliberately generous (many phrasings match) because the RISK
# consequence of a false positive here is zero: a timeframe-free thesis
# that slips through still has to clear select_contract + sizing + the
# full risk engine before anything trades. Denying too eagerly just
# costs the agent a wasted round; denying too rarely costs nothing at
# all, since nothing downstream trusts this thesis for risk purposes.
_TIMEFRAME_RE = re.compile(
    r"\b\d+[\s-]?(day|days|week|weeks|month|months|session|sessions|hour|hours)\b"
    r"|\b(today|tomorrow|overnight|intraday|eod|eow)\b"
    r"|\bby\s+(close|expiry|expiration|friday|monday|tuesday|wednesday|thursday|"
    r"saturday|sunday|the\s+close|next\s+week|month\s+end|year\s+end)\b"
    r"|\b(within|over|before)\s+the\s+next\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

# Bounds an agent may even ASK for (docs/IMPL_OPTIONS_AGENTS.md §1 schema +
# docs/PLAN_OPTIONS_AGENTS.md §3.3's table). Matches OPEN_OPTION_TRADE's
# JSON-schema minimum/maximum exactly — re-enforced here because Anthropic's
# tool-use schema is a HINT to the model, not a server-side validator; a
# malformed or adversarial completion could still emit an out-of-band
# number, so this file clamps defensively rather than trusting the schema.
_STOP_LOSS_BAND = (25.0, 50.0)
_TAKE_PROFIT_BAND = (40.0, 300.0)

_MAX_SCALE_IN_ADDS = 2

# Belt-and-suspenders alongside OptionLegDetails.action's type-level
# Literal restriction and the risk engine's own naked_short_forbidden rule
# (engine.options.rules) — Phase A never constructs anything but a bought
# call/put. Every OptionLegDetails this file builds hardcodes
# action="buy_to_open" already, so this can only ever fire if that
# invariant is broken elsewhere; the point of checking it explicitly here
# is that a test can prove the check exists and would catch a regression,
# not that it is reachable today.
_ALLOWED_ACTIONS = frozenset({"buy_to_open"})

_ADJUST_ACTIONS = frozenset(
    {"SCALE_IN", "EXIT_NOW", "RAISE_TAKE_PROFIT", "TIGHTEN_STOP", "HOLD"}
)

# Derived from schemas.py's own READ_ONLY_TOOLS rather than hand-listed a
# second time (CLAUDE.md §4.4: the same list in two places will drift) —
# these six names are always allowed through unconditionally. They carry
# no risk (nothing here ever reaches packages/broker; see tools/readonly.py)
# and self-scope to ctx.user_id inside each handler, so there is no 12-step
# stack for before() to run for them — only the two mutating tools have one.
_READ_ONLY_TOOL_NAMES = frozenset(t["name"] for t in READ_ONLY_TOOLS)


# ─────────────────────────────────────────────────────────────────────
# Small pure helpers
# ─────────────────────────────────────────────────────────────────────


def _parses_timeframe(thesis: str) -> bool:
    return bool(thesis) and _TIMEFRAME_RE.search(thesis) is not None


def _trading_mode() -> str:
    """Duplicates ``app.services.orders.paper_broker.trading_mode()``'s
    exact semantics: ``"live"`` only if TRADING_MODE is literally that
    string, else ``"paper"``.

    Reimplemented, not imported: ``apps/agents/pyproject.toml`` depends on
    only ``broker`` + ``engine`` as workspace siblings, not ``apps/api`` —
    this package is deliberately decoupled from the API app (see
    ``apps/agents/trading_agents/jobs/daily_cron.py``'s ``_notify_proposal``
    for the one place this repo DOES reach across that boundary, and note
    it does so via a best-effort ``try/except: pass`` lazy import, which is
    the wrong shape for the single hard-coded safety check in this file —
    a missing/broken import must never be interpreted as "safe to trade".
    Six lines of env-reading duplicated beats one import that can fail
    open.
    """
    mode = os.environ.get("TRADING_MODE", "").strip().lower()
    return mode if mode in ("paper", "live") else "paper"


def _is_paper_and_safe() -> bool:
    """``trading_mode()=="paper" and not LIVE_TRADING_ENABLED`` — hard-coded
    per docs/IMPL_OPTIONS_AGENTS.md §2.1 step 2 and
    docs/PLAN_OPTIONS_AGENTS.md §4. NEVER read from a table, a per-user
    flag, or anything reconfigurable. This is the one check in the whole
    stack where paper accidentally trading live would be exactly the
    disaster CLAUDE.md's "You may not execute trades" boundary exists to
    prevent."""
    return _trading_mode() == "paper" and not env_flag("LIVE_TRADING_ENABLED")


def _alpaca_credentials() -> tuple[str, str]:
    """Mirrors ``AlpacaBroker.from_env()``'s own key resolution exactly
    (including the ``ALPACA_API_SECRET`` legacy alias) — duplicated here
    because this file needs the raw strings for
    ``engine.options.contracts.fetch_option_candidates`` too, not just for
    constructing a broker client."""
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = (
        os.environ.get("ALPACA_SECRET_KEY", "").strip()
        or os.environ.get("ALPACA_API_SECRET", "").strip()
    )
    return key, secret


def _as_float(value: Any, *, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _default_broker() -> Any:
    # Lazy import: a MOCK-mode/offline deployment must not need alpaca-py
    # importable just because this module is.
    from broker.alpaca import AlpacaBroker

    return AlpacaBroker.from_env()


# ─────────────────────────────────────────────────────────────────────
# Frozen shapes (docs/IMPL_OPTIONS_AGENTS.md §2)
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuardVerdict:
    allow: bool
    reason: str | None = None
    """Named, like a risk veto rule — never a free-text explanation."""
    payload: dict[str, Any] | None = None
    """``before()``'s payload carries whatever the matching ``trade.py``
    handler needs to act blindly (the selected contract, qty, resolved
    infra). ``after()``'s payload may only NARROW a result, never widen
    one — see ``ToolGuard.after``."""


@dataclass(frozen=True)
class GuardContext:
    user_id: str
    council_run_id: str
    resolved_direction: str | None
    resolved_conviction: float | None
    calls_this_pass: int
    caps: RiskCaps


# ─────────────────────────────────────────────────────────────────────
# ToolGuard
# ─────────────────────────────────────────────────────────────────────


class ToolGuard:
    def __init__(
        self,
        *,
        context_provider: RiskContextProvider | None = None,
        decision_log: Any | None = None,
        session_factory: Any | None = None,
        broker_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Every dependency is optional and lazily resolved on first use —
        constructing ``ToolGuard()`` with no arguments must stay cheap and
        must not touch a database or a broker until a call actually needs
        one. Tests inject fakes for all five; production leaves them None
        and gets the env-driven real resolution (mirrors
        ``trading_agents.nodes.risk_officer._default_provider``'s
        ``USE_POSTGRES`` switch)."""
        self._context_provider = context_provider
        self._decision_log = decision_log
        self._session_factory = session_factory
        self._broker_factory = broker_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    # ── infra resolution ────────────────────────────────────────────

    def _resolve_context_provider(self) -> RiskContextProvider:
        if self._context_provider is not None:
            return self._context_provider
        if env_flag("USE_POSTGRES"):
            from engine.db import async_session_factory

            return PostgresRiskContextProvider(session_factory=async_session_factory())
        return MockRiskContextProvider()

    def _resolve_decision_log(self) -> Any:
        if self._decision_log is not None:
            return self._decision_log
        from trading_agents.memory import get_decision_log

        return get_decision_log()

    def _resolve_session_factory(self) -> Any:
        """``None`` in non-Postgres mode — there is no real row to
        jsonb_set against, so callers treat ``None`` as "skip persistence,
        log it" rather than crashing a dry-run/offline pass."""
        if self._session_factory is not None:
            return self._session_factory
        if not env_flag("USE_POSTGRES"):
            return None
        from engine.db import async_session_factory

        return async_session_factory()

    def _resolve_broker_factory(self) -> Callable[[], Any]:
        return self._broker_factory or _default_broker

    # ── before() ─────────────────────────────────────────────────────

    async def before(
        self, tool: str, args: Mapping[str, Any], ctx: GuardContext
    ) -> GuardVerdict:
        if tool == "open_option_trade":
            return await self._before_open_option_trade(args, ctx)
        if tool == "adjust_option_position":
            return await self._before_adjust_option_position(args, ctx)
        if tool in _READ_ONLY_TOOL_NAMES:
            # No risk stack to run — these tools never reach packages/broker
            # and self-scope to ctx.user_id inside their own handler
            # (tools/readonly.py). An empty payload, not None: after()
            # treats a dict the same as any other handler result.
            return GuardVerdict(True, None, {})
        return GuardVerdict(False, "unknown_tool")

    async def _before_open_option_trade(
        self, args: Mapping[str, Any], ctx: GuardContext
    ) -> GuardVerdict:
        # 1. Master switch.
        if not env_flag("AUTO_TRADE_ENABLED"):
            return GuardVerdict(False, "auto_trade_disabled")
        # 2. Paper-only, hard-coded.
        if not _is_paper_and_safe():
            return GuardVerdict(False, "live_mode_refused")

        now = self._clock()
        # 3. Market open.
        if not is_us_market_open(now):
            return GuardVerdict(False, "market_closed")
        # 4. One open_option_trade per pass. GuardContext carries a single
        # counter, not one keyed by symbol — but this system only ever
        # runs one council pass per underlying (CLAUDE.md's architecture),
        # so "one per symbol per pass" and "one per pass" coincide exactly
        # in how this is actually invoked.
        if ctx.calls_this_pass >= 1:
            return GuardVerdict(False, "one_open_per_pass")

        underlying = str(args.get("underlying", "")).strip().upper()
        # 5. Symbol shape.
        if not SYMBOL_RE.match(underlying):
            return GuardVerdict(False, "malformed_symbol")
        # 6. Strategy registered.
        strategy = str(args.get("strategy", ""))
        if strategy not in STRATEGY_REGISTRY:
            return GuardVerdict(False, "unknown_strategy")
        # 7. Direction matches the resolution the two agents already
        # reached — a contradicting tool call (or a missing resolution)
        # is denied, never guessed at.
        direction = str(args.get("direction", ""))
        if direction not in ("long", "short") or direction != ctx.resolved_direction:
            return GuardVerdict(False, "direction_contradicts_resolution")
        # 8. Thesis parses a timeframe.
        thesis = str(args.get("thesis", ""))
        if not _parses_timeframe(thesis):
            return GuardVerdict(False, "thesis_without_timeframe")

        # 9. Clamp agent-settable bounds.
        conviction = _clamp(_as_float(args.get("conviction"), default=0.0) or 0.0, 0.0, 1.0)
        if ctx.resolved_conviction is not None:
            # "min, not the mean" (resolution.py) applies again HERE, not
            # just at resolution time — an agent re-asserting a higher
            # conviction via the tool call than the two-agent resolution
            # actually reached would drag the delta band toward the money
            # on its own say-so. Cheap, defense-in-depth, costs nothing.
            conviction = min(conviction, ctx.resolved_conviction)
        take_profit_pct = _clamp(
            _as_float(args.get("take_profit_pct"), default=_TAKE_PROFIT_BAND[0])
            or _TAKE_PROFIT_BAND[0],
            *_TAKE_PROFIT_BAND,
        )
        stop_loss_pct = _clamp(
            _as_float(args.get("stop_loss_pct"), default=_STOP_LOSS_BAND[1])
            or _STOP_LOSS_BAND[1],
            *_STOP_LOSS_BAND,
        )

        caps = ctx.caps
        api_key, secret_key = _alpaca_credentials()
        if not api_key or not secret_key:
            return GuardVerdict(False, "broker_credentials_unavailable")

        # 10. select_contract -> None names the funnel rejection.
        try:
            candidates = await fetch_option_candidates(
                underlying, api_key=api_key, secret_key=secret_key, now=now, caps=caps
            )
        except Exception:
            logger.exception("guard: chain fetch failed for %s", underlying)
            return GuardVerdict(False, "chain_fetch_failed")

        selection = select_contract(
            ContractSelectionInputs(
                underlying_symbol=underlying,
                direction=direction,  # type: ignore[arg-type]
                conviction=conviction,
                candidates=candidates,
                now=now,
            )
        )
        if selection.selected is None:
            return GuardVerdict(False, selection.rejection_reason or "no_liquid_contract")

        option = selection.selected
        if option.action not in _ALLOWED_ACTIONS:
            return GuardVerdict(False, "naked_short_forbidden")
        ask = option.ask
        if ask is None or ask <= 0:
            return GuardVerdict(False, "no_liquid_contract")

        try:
            context = await self._resolve_context_provider().fetch(user_id=ctx.user_id)
        except Exception:
            logger.exception("guard: risk-context fetch failed for user %s", ctx.user_id)
            return GuardVerdict(False, "context_fetch_failed")

        # 11. options_position_size -> qty<1 is size_rounds_to_zero.
        budget_usd = context.account_equity * caps.options_max_premium_pct / 100.0
        sizing = options_position_size(
            OptionsSizingInputs(budget_usd=budget_usd, ask=ask, multiplier=option.multiplier)
        )
        if sizing.qty < 1:
            return GuardVerdict(False, "size_rounds_to_zero")

        proposal = to_risk_proposal(
            symbol=underlying,
            side=Side.BUY,
            qty=sizing.qty,
            estimated_notional=round(sizing.qty * ask * option.multiplier, 2),
            # PER-CONTRACT PREMIUM, never the underlying's share price —
            # this exact confusion vetoed 100% of options proposals for
            # weeks (OPTIONS_PLAYBOOK.md §5.2). `ask` here comes straight
            # off the selected contract, never off an underlying quote.
            last_price=ask,
            confidence=conviction,
            option=option,
        )
        # 12. The full risk engine — the SAME evaluate() the executor and
        # risk_officer_node already call for every proposal. Not
        # reimplemented, not bypassed.
        decision = evaluate(proposal, context, caps)
        if not decision.approved:
            return GuardVerdict(False, decision.veto_rule or "risk_vetoed")

        final_qty = decision.adjusted_qty if decision.adjusted_qty is not None else sizing.qty

        payload = {
            "underlying": underlying,
            "direction": direction,
            "strategy": strategy,
            "conviction": conviction,
            "thesis": thesis,
            "take_profit_pct": take_profit_pct,
            "stop_loss_pct": stop_loss_pct,
            "option": option,
            "qty": final_qty,
            "limit_price": ask,
            "risk_decision": decision,
            "context_used": context,
            "council_run_id": ctx.council_run_id,
            "user_id": ctx.user_id,
            "broker_factory": self._resolve_broker_factory(),
            "decision_log": self._resolve_decision_log(),
            "session_factory": self._resolve_session_factory(),
            "now": now,
        }
        return GuardVerdict(True, None, payload)

    async def _before_adjust_option_position(
        self, args: Mapping[str, Any], ctx: GuardContext
    ) -> GuardVerdict:
        action = str(args.get("action", ""))
        if action not in _ADJUST_ACTIONS:
            return GuardVerdict(False, "unknown_action")

        decision_id = str(args.get("decision_id", ""))
        row = await self._load_open_option_decision(decision_id, ctx.user_id)
        if row is None:
            return GuardVerdict(False, "decision_not_found")

        option_state: dict[str, Any] = dict(
            (row.get("reasoning") or {}).get("option_exit") or {}
        )
        current_stop = _as_float(
            option_state.get("stop_loss_pct"), default=ctx.caps.options_stop_loss_pct
        )
        current_tp = _as_float(
            option_state.get("take_profit_pct"), default=ctx.caps.options_take_profit_pct
        )
        adds_so_far = int(option_state.get("adds_this_position", 0) or 0)

        base_payload: dict[str, Any] = {
            "decision_id": decision_id,
            "user_id": ctx.user_id,
            "action": action,
            "occ_symbol": row.get("occ_symbol"),
            "underlying": row.get("underlying"),
            "decision_log": self._resolve_decision_log(),
            "session_factory": self._resolve_session_factory(),
            "broker_factory": self._resolve_broker_factory(),
            "now": self._clock(),
        }

        # EXIT_NOW / HOLD: de-risking (or doing nothing) is never blocked.
        if action in ("EXIT_NOW", "HOLD"):
            return GuardVerdict(True, None, {**base_payload, "option_state": option_state})

        if action == "TIGHTEN_STOP":
            value = _as_float(args.get("value"), default=None)
            if value is None:
                return GuardVerdict(False, "value_required")
            value = _clamp(value, *_STOP_LOSS_BAND)
            # See this module's docstring "sign convention" note: tighter
            # == a SMALLER stop_loss_pct magnitude, matching
            # RiskCaps.options_stop_loss_pct's established meaning and
            # OPTIONS_PLAYBOOK.md §2's "50.0 -> 40.0, cut losers early"
            # precedent. A value that is not strictly smaller than the
            # current one never reaches the broker or the audit log as a
            # change — it is denied and the position keeps whatever
            # protection it already had.
            if current_stop is None or not (0 < value < current_stop):
                return GuardVerdict(False, "cannot_loosen_protection")
            new_state = {**option_state, "stop_loss_pct": value}
            return GuardVerdict(
                True, None, {**base_payload, "option_state": new_state, "value": value}
            )

        if action == "RAISE_TAKE_PROFIT":
            value = _as_float(args.get("value"), default=None)
            if value is None:
                return GuardVerdict(False, "value_required")
            value = _clamp(value, *_TAKE_PROFIT_BAND)
            if current_tp is None or not (value > current_tp):
                return GuardVerdict(False, "cannot_loosen_protection")
            new_state = {**option_state, "take_profit_pct": value}
            return GuardVerdict(
                True, None, {**base_payload, "option_state": new_state, "value": value}
            )

        if action == "SCALE_IN":
            return await self._before_scale_in(
                args, ctx, row=row, option_state=option_state, adds_so_far=adds_so_far,
                base_payload=base_payload,
            )

        return GuardVerdict(False, "unknown_action")  # pragma: no cover — unreachable

    async def _before_scale_in(
        self,
        args: Mapping[str, Any],
        ctx: GuardContext,
        *,
        row: dict[str, Any],
        option_state: dict[str, Any],
        adds_so_far: int,
        base_payload: dict[str, Any],
    ) -> GuardVerdict:
        if adds_so_far >= _MAX_SCALE_IN_ADDS:
            return GuardVerdict(False, "scale_in_cap_reached")

        occ_symbol = str(row.get("occ_symbol") or "")
        underlying = str(row.get("underlying") or "")
        from broker.types import OccSymbol

        occ = OccSymbol.try_parse(occ_symbol)
        if occ is None or not underlying:
            return GuardVerdict(False, "contract_unavailable")

        api_key, secret_key = _alpaca_credentials()
        if not api_key or not secret_key:
            return GuardVerdict(False, "broker_credentials_unavailable")

        now = self._clock()
        try:
            candidates = await fetch_option_candidates(
                underlying, api_key=api_key, secret_key=secret_key, now=now, caps=ctx.caps
            )
        except Exception:
            logger.exception("guard: scale-in chain fetch failed for %s", underlying)
            return GuardVerdict(False, "chain_fetch_failed")

        match = next((c for c in candidates if c.occ_symbol == occ_symbol), None)
        if match is None or match.ask is None or match.ask <= 0:
            return GuardVerdict(False, "contract_unavailable")

        try:
            context = await self._resolve_context_provider().fetch(user_id=ctx.user_id)
        except Exception:
            logger.exception("guard: risk-context fetch failed for user %s", ctx.user_id)
            return GuardVerdict(False, "context_fetch_failed")

        budget_usd = context.account_equity * ctx.caps.options_max_premium_pct / 100.0
        sizing = options_position_size(
            OptionsSizingInputs(budget_usd=budget_usd, ask=match.ask, multiplier=100)
        )
        if sizing.qty < 1:
            return GuardVerdict(False, "size_rounds_to_zero")

        option = OptionLegDetails(
            underlying_symbol=underlying,
            occ_symbol=occ_symbol,
            contract_type=occ.contract_type,  # type: ignore[arg-type]
            strike=occ.strike,
            expiry=occ.expiry,
            multiplier=100,
            action="buy_to_open",
            open_interest=match.open_interest,
            volume=match.volume,
            bid=match.bid,
            ask=match.ask,
            implied_volatility=match.implied_volatility,
        )
        if option.action not in _ALLOWED_ACTIONS:
            return GuardVerdict(False, "naked_short_forbidden")  # pragma: no cover

        proposal = to_risk_proposal(
            symbol=underlying,
            side=Side.BUY,
            qty=sizing.qty,
            estimated_notional=round(sizing.qty * match.ask * 100, 2),
            last_price=match.ask,
            confidence=1.0,  # an add re-affirms the same, already-resolved thesis
            option=option,
        )
        # Full risk re-check — max_total_premium_pct naturally sees this
        # add ON TOP of the existing position because `context` reflects
        # current broker state. No separate aggregate-tracking needed here.
        decision = evaluate(proposal, context, ctx.caps)
        if not decision.approved:
            return GuardVerdict(False, decision.veto_rule or "risk_vetoed")

        final_qty = decision.adjusted_qty if decision.adjusted_qty is not None else sizing.qty
        new_state = {**option_state, "adds_this_position": adds_so_far + 1}
        return GuardVerdict(
            True,
            None,
            {
                **base_payload,
                "option_state": new_state,
                "qty": final_qty,
                "limit_price": match.ask,
                "risk_decision": decision,
            },
        )

    async def _load_open_option_decision(
        self, decision_id: str, user_id: str
    ) -> dict[str, Any] | None:
        session_factory = self._resolve_session_factory()
        if session_factory is None:
            return None
        try:
            did = uuid.UUID(decision_id)
            uid = uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            return None

        from engine.db.models import AgentDecision

        async with session_factory() as session:
            row = await session.get(AgentDecision, did)
        # Ownership check mirrors position_manager.close_position_now
        # exactly: a user can never touch another user's position, and a
        # row missing entirely reads the same as "not found" either way.
        if row is None or row.user_id != uid:
            return None
        if row.closed_at is not None:
            return None
        proposal = row.proposal or {}
        occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
        return {
            "occ_symbol": str(occ) if occ else None,
            "underlying": row.symbol,
            "reasoning": row.reasoning or {},
            "fill_qty": row.fill_qty,
        }

    # ── after() ──────────────────────────────────────────────────────

    async def after(
        self, tool: str, args: Mapping[str, Any], result: Any, ctx: GuardContext
    ) -> GuardVerdict:
        """Persist the audit row, then narrow-never-widen ``result``.

        ``result`` is the raw return value of the ``trade.py`` handler
        (never the ``is_error``/denied envelope — that only exists once
        ``dispatch_tool_call`` wraps a DENIAL from ``before()``, which
        never reaches a handler at all). ``dispatch_tool_call`` stashes a
        ``_latency_ms`` key into ``result`` before calling this — the
        frozen ``after(tool, args, result, ctx)`` signature has no room
        for a fifth parameter, so this is the least-invasive way to get
        timing into the audit row without changing the spec'd signature.
        """
        result_dict: dict[str, Any] = dict(result) if isinstance(result, dict) else {"value": result}
        latency_ms = result_dict.pop("_latency_ms", None)

        allow = True
        reason: str | None = None

        # Defense-in-depth: after() may only narrow. If a handler bug
        # somehow returned another tenant's row, never let it out — even
        # though neither of THIS file's two handlers currently query
        # cross-tenant data, a future handler mistake must not silently
        # leak through an audit layer whose entire job is to catch this
        # class of error.
        row_user_id = result_dict.get("user_id")
        if row_user_id is not None and str(row_user_id) != str(ctx.user_id):
            allow = False
            reason = "user_scope_violation"
            result_dict = {}

        await self._persist_tool_log(
            tool=tool,
            args=dict(args),
            allow=allow,
            reason=reason,
            latency_ms=latency_ms,
            ctx=ctx,
        )

        if allow and tool == "open_option_trade" and result_dict.get("decision_id"):
            await self._stamp_auto_approval(
                decision_id=str(result_dict["decision_id"]), user_id=ctx.user_id
            )

        payload = _truncate_payload(result_dict)
        return GuardVerdict(allow=allow, reason=reason, payload=payload)

    async def _persist_tool_log(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        allow: bool,
        reason: str | None,
        latency_ms: float | None,
        ctx: GuardContext,
    ) -> None:
        session_factory = self._resolve_session_factory()
        if session_factory is None:
            logger.debug(
                "tool_log: no session factory (non-Postgres mode) — skipping audit "
                "persistence for %s",
                tool,
            )
            return

        decision_id = args.get("decision_id") or ctx.council_run_id
        try:
            did = uuid.UUID(str(decision_id))
        except (ValueError, TypeError):
            logger.warning(
                "tool_log: %r is not a UUID — skipping audit row for %s", decision_id, tool
            )
            return

        row = {
            "tool": tool,
            "args": args,
            "allow": allow,
            "reason": reason,
            "latency_ms": latency_ms,
            "at": self._clock().isoformat(),
        }
        stmt, params = _tool_log_append_stmt(decision_id=did, row=row)
        async with session_factory() as session:
            await session.execute(stmt, params)
            await session.commit()

    async def _stamp_auto_approval(self, *, decision_id: str, user_id: str) -> None:
        """The only evidence in the audit log that this trade placed with
        no human in the loop — mirrors
        ``app.services.orders.auto_approver._stamp_auto_approval`` (same
        intent, matched by row id directly here since ``trade.py`` always
        has the real row id in hand, rather than that function's
        JSONB-``proposal->id`` match, which exists there only because it
        doesn't)."""
        session_factory = self._resolve_session_factory()
        if session_factory is None:
            return
        try:
            did = uuid.UUID(str(decision_id))
            uid = uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            return

        from sqlalchemy import update

        from engine.db.models import AgentDecision

        async with session_factory() as session:
            await session.execute(
                update(AgentDecision)
                .where(AgentDecision.id == did, AgentDecision.user_id == uid)
                .values(
                    approval_mode="auto",
                    user_response="approved",
                    user_responded_at=self._clock(),
                )
            )
            await session.commit()


# ─────────────────────────────────────────────────────────────────────
# JSONB partial-update statements — split from their execute wrappers so
# the emitted SQL + bound params are directly assertable with no live
# Postgres involved. Mirrors position_manager.py's
# ``_option_exit_peak_update_stmt`` / ``_persist_option_exit_peak`` split
# exactly (same jsonb_set + COALESCE shape, same reasoning for both).
# ─────────────────────────────────────────────────────────────────────


def _tool_log_append_stmt(*, decision_id: uuid.UUID, row: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Append-only: concatenates ONE new row onto whatever ``tool_log``
    array already exists, touching no sibling key in ``reasoning``
    (``contract_funnel``, ``option_exit``, ``risk_profile``, …).
    ``COALESCE`` on the array itself handles "no tool_log yet" without a
    separate INSERT-vs-UPDATE branch; ``COALESCE`` on the whole column
    handles a NULL ``reasoning`` the same way
    ``_option_exit_peak_update_stmt`` does (``jsonb_set(NULL, ...)`` is
    NULL in Postgres — silently blanking the column is exactly the
    whole-column-overwrite bug this function exists to avoid)."""
    return (
        text(
            "UPDATE agent_decisions "
            "SET reasoning = jsonb_set("
            "COALESCE(reasoning, '{}'::jsonb), '{tool_log}', "
            "COALESCE(reasoning->'tool_log', '[]'::jsonb) || CAST(:new_row AS jsonb), "
            "true"
            ") "
            "WHERE id = :id"
        ),
        {"new_row": json.dumps([row], default=str), "id": str(decision_id)},
    )


def _option_exit_merge_stmt(*, decision_id: uuid.UUID, state: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Whole-VALUE replace of the ``option_exit`` key only (never of the
    ``reasoning`` column). Callers merge over the existing dict themselves
    before calling this — mirrors ``_persist_option_exit_peak``'s own
    merge-before-write convention — so a write here can never regress a
    key ``position_manager.py``'s ratchet already owns (``peak_pl_pct`` /
    ``armed`` / ``trail_line_pct`` survive because the caller read them
    into ``state`` first)."""
    return (
        text(
            "UPDATE agent_decisions "
            "SET reasoning = jsonb_set("
            "COALESCE(reasoning, '{}'::jsonb), '{option_exit}', "
            "CAST(:payload AS jsonb), true"
            ") "
            "WHERE id = :id"
        ),
        {"payload": json.dumps(state, default=str), "id": str(decision_id)},
    )


async def persist_option_state(
    session_factory: Any, *, decision_id: str, state: dict[str, Any]
) -> None:
    """Module-level (not a ``ToolGuard`` method) so ``tools/trade.py`` can
    reuse the exact same jsonb_set shape without constructing a
    ``ToolGuard`` just to reach it."""
    if session_factory is None:
        return
    try:
        did = uuid.UUID(str(decision_id))
    except (ValueError, TypeError):
        return
    stmt, params = _option_exit_merge_stmt(decision_id=did, state=state)
    async with session_factory() as session:
        await session.execute(stmt, params)
        await session.commit()


async def stamp_position_closed(
    session_factory: Any, *, decision_id: str, user_id: str, reason: str, now: datetime
) -> None:
    """Mirrors the plain-column half of ``position_manager.py``'s close
    path (``closed_at`` / ``close_reason``) — used by ``trade.py``'s
    ``EXIT_NOW`` handler. Deliberately does not touch ``fill_qty`` /
    realized P&L; that reconciliation is the SAME broker-fill-driven path
    every other close in this system already goes through, not something
    this file re-derives."""
    if session_factory is None:
        return
    try:
        did = uuid.UUID(str(decision_id))
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return

    from sqlalchemy import update

    from engine.db.models import AgentDecision

    async with session_factory() as session:
        await session.execute(
            update(AgentDecision)
            .where(AgentDecision.id == did, AgentDecision.user_id == uid)
            .values(closed_at=now, close_reason=reason)
        )
        await session.commit()


def _truncate_payload(payload: dict[str, Any], limit: int = 8192) -> dict[str, Any]:
    try:
        encoded = json.dumps(payload, default=str)
    except Exception:
        return payload
    if len(encoded.encode("utf-8")) <= limit:
        return payload
    return {
        "truncated": True,
        "original_keys": list(payload.keys()),
        "note": f"payload exceeded {limit} bytes — truncated before re-entering the context window",
    }


# ─────────────────────────────────────────────────────────────────────
# dispatch_tool_call — docs/IMPL_OPTIONS_AGENTS.md §2.4, "dispatch never
# raises". This is the function ``run_tool_loop``'s ``dispatch`` callable
# is built from (``options/agents.py``, a separate workstream, wires
# ``functools.partial`` or a closure over ``registry.REGISTRY`` + a
# ``ToolGuard`` instance + a ``GuardContext`` onto this).
# ─────────────────────────────────────────────────────────────────────


async def dispatch_tool_call(
    call: Any,
    ctx: GuardContext,
    *,
    guard: ToolGuard,
    registry: Mapping[str, Callable[[dict[str, Any], GuardContext, dict[str, Any]], Awaitable[dict[str, Any]]]],
) -> dict[str, Any]:
    """A denial teaches the model (``is_error: true`` + the named reason)
    and the pass continues; nothing here ever raises into the caller.
    ``run_tool_loop`` (``trading_agents.llm_loop``) already wraps its own
    call to a ``dispatch`` callable in a try/except as a second layer of
    the same protection — this function does not rely on that outer net
    and enforces "never raises" on its own terms, so it is independently
    testable.

    ``call`` is duck-typed to ``trading_agents.llm.ToolCall`` (``.id``,
    ``.name``, ``.input``) rather than importing that type directly, to
    avoid a needless import-time coupling from this options-only module
    back to the shared ``llm`` module.
    """
    handler = registry.get(call.name)
    if handler is None:
        return {"is_error": True, "content": {"denied": "unknown_tool"}}

    try:
        verdict = await guard.before(call.name, call.input, ctx)
    except Exception:
        logger.exception("guard.before raised for tool %r — denying, never raising", call.name)
        return {"is_error": True, "content": {"denied": "guard_error"}}

    if not verdict.allow:
        return {"is_error": True, "content": {"denied": verdict.reason}}

    start = datetime.now(UTC)
    try:
        result = await handler(dict(call.input), ctx, verdict.payload or {})
    except Exception:
        logger.exception("tool %s failed", call.name)
        return {"is_error": True, "content": {"denied": "tool_failed"}}
    latency_ms = (datetime.now(UTC) - start).total_seconds() * 1000.0

    result_with_latency = {**result, "_latency_ms": latency_ms} if isinstance(result, dict) else result

    try:
        after_verdict = await guard.after(call.name, call.input, result_with_latency, ctx)
    except Exception:
        logger.exception("guard.after raised for tool %r — denying, never raising", call.name)
        return {"is_error": True, "content": {"denied": "guard_error"}}

    if not after_verdict.allow:
        return {"is_error": True, "content": {"denied": after_verdict.reason}}
    return {
        "is_error": False,
        "content": after_verdict.payload if after_verdict.payload is not None else result,
    }
