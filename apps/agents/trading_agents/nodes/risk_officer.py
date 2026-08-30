"""Risk Officer node — thin adapter over ``engine.risk.evaluate``.

Phase 1 graduation: the deterministic ruleset now lives in
``packages/engine/engine/risk/`` (rules, evaluator, types, context provider).
This node:

  1. Builds a ``RiskProposal`` from the council's draft.
  2. Fetches a ``RiskContext`` from the injected provider
     (``MockRiskContextProvider`` by default; production wires in the real
     reconciler-backed provider).
  3. Calls ``evaluate`` and surfaces the result into ``CouncilState``.

Architecture rule honored: NO LLM here. Risk vetoes are pure Python with
named ``veto_rule`` strings. The PLAN.md §5.1 "Opus refinement" of risk
reasoning is a future, additive layer — it can explain, never override.

Short-side inputs come from the FEATURE dict, not from the model: the
proposal's stop price (computed by the sizer) and the broker's
``shortable`` / ``easy_to_borrow`` flags (fetched by the feature provider's
asset block). If the asset block is missing, both flags arrive as None and
``shortable_check`` vetoes — which is the correct behaviour, because a
short with no verified borrow is not a trade.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal, cast

from engine.env import env_flag
from engine.risk import (
    MockRiskContextProvider,
    RiskCaps,
    RiskContextProvider,
    RiskProposal,
    Side,
    SpecialistScore,
    evaluate,
)
from engine.risk.types import OptionLegDetails
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.node.risk")


def _specialists_from_state(state: CouncilState) -> list[SpecialistScore]:
    out: list[SpecialistScore] = []
    for name in ("technical", "fundamental", "macro"):
        s = cast(dict, state.get(name))
        if s and "score" in s:
            out.append(
                SpecialistScore(
                    name=name,
                    score=float(s.get("score", 0.0)),
                    confidence=float(s.get("confidence", 0.0)),
                )
            )
    return out


async def risk_officer_node(
    state: CouncilState,
    caps: RiskCaps | None = None,
    *,
    context_provider: RiskContextProvider | None = None,
) -> CouncilState:
    # from_env, not RiskCaps(): this is the call site that decides whether
    # ALLOW_SHORTS is honored at all. Defaults to long-only when unset.
    caps = caps or RiskCaps.from_env()
    provider = context_provider or _default_provider(state)

    proposal = state.get("proposal")
    if proposal is None:
        return {
            **state,
            "risk_approved": False,
            "risk_reason": "No proposal — HOLD.",
            "risk_veto_rule": None,
        }

    asset = (state.get("context", {}) or {}).get("asset") or {}
    is_option = bool(proposal.get("is_option", False))
    # ``last_price`` means "the price one unit of the thing being risked
    # costs". For equities that is the share price; for an option it is the
    # per-CONTRACT premium. Passing the underlying's share price here made
    # ``max_premium_pct`` compute premium = last_price * multiplier, turning
    # a $229 underlying into a $22,900 "premium" and vetoing every single
    # options proposal the council ever produced (live: "68.71% of equity,
    # cap 1.00%" on a position whose real premium was 0.96%).
    #
    # Same rule the executor's re-risk-check already documents in
    # ``_option_risk_proposal``: the premium is ``limit_price`` (the ask the
    # contract was selected and sized against), never a reverse-computed
    # ``estimated_notional / qty``, which is a per-share equity price
    # carrying no multiplier.
    underlying_last = float(state.get("context", {}).get("last_price", 0.0) or 0.0)
    if is_option:
        last_price = (
            _opt_float(proposal.get("limit_price"))
            or _opt_float(proposal.get("ask"))
            or 0.0
        )
    else:
        last_price = underlying_last
    risk_proposal = RiskProposal(
        symbol=str(state["symbol"]),
        side=Side(str(proposal.get("side", "BUY")).upper()),
        qty=int(proposal.get("qty", 0)),
        estimated_notional=float(proposal.get("estimated_notional", 0.0)),
        last_price=last_price,
        confidence=float(proposal.get("confidence", 0.0)),
        closes_intraday_position=False,  # Phase 0: agents only open new swings
        is_option=is_option,
        option=_option_details_from_proposal(proposal, symbol=str(state["symbol"])) if is_option else None,
        # Short-side inputs. ``stop_price`` is the sizer's, never the LLM's.
        stop_price=_opt_float(proposal.get("stop_loss")),
        shortable=_opt_bool(asset.get("shortable")),
        easy_to_borrow=_opt_bool(asset.get("easy_to_borrow")),
    )

    context = await provider.fetch(user_id=state.get("user_id"))
    decision = evaluate(risk_proposal, context, caps, specialists=_specialists_from_state(state))

    out: CouncilState = {
        **state,
        "risk_approved": decision.approved,
        "risk_reason": decision.reason,
        "risk_veto_rule": decision.veto_rule,
        # Named rules that ran and did not block. A veto explains a refusal;
        # this explains an approval, which is what the user actually sees.
        "risk_checks_passed": list(decision.checks_passed),
        # Rules that shrank the trade rather than blocking it — a partial
        # refusal, and the most common kind. Kept separate from
        # ``risk_veto_rule`` so the ledger never counts a trim as a block.
        "risk_trim_rules": list(decision.trim_rules),
    }

    if not decision.approved:
        out["final_action"] = "VETOED"
        logger.info(
            "risk vetoed %s %s qty=%d via %s — %s",
            risk_proposal.side.value, risk_proposal.symbol, risk_proposal.qty,
            decision.veto_rule, decision.reason,
        )
        return out

    # Approved — may have a trim and/or informational flags.
    new_proposal: dict | None = None
    if decision.adjusted_qty is not None and decision.adjusted_qty != risk_proposal.qty:
        new_proposal = dict(proposal)
        new_proposal["qty"] = decision.adjusted_qty
        # An option's notional is qty * premium * multiplier — dropping the
        # multiplier understates it 100x. This is not cosmetic: the veto
        # ledger sums ``estimatedNotional`` into ``blocked_notional``, which
        # is a headline number on the Refusal Ledger.
        trim_multiplier = int(proposal.get("multiplier", 100)) if is_option else 1
        new_proposal["estimated_notional"] = round(
            decision.adjusted_qty * risk_proposal.last_price * trim_multiplier, 2
        )
        new_proposal["rationale"] = (
            (proposal.get("rationale") or "")
            + f" (Risk trim: {risk_proposal.qty}→{decision.adjusted_qty})"
        ).strip()

    # Surface non-blocking flags (e.g. wash_sale_warning) onto the proposal
    # so the wire DTO + ApprovalCard can render them. Only forward UI-relevant
    # flags — internal markers like 'trimmed:80->37' stay in the audit log.
    ui_flags = [f for f in decision.informational_flags if not f.startswith("trimmed:")]
    if ui_flags:
        new_proposal = new_proposal if new_proposal is not None else dict(proposal)
        existing = list(new_proposal.get("informational_flags") or [])
        # de-dup while preserving order
        for f in ui_flags:
            if f not in existing:
                existing.append(f)
        new_proposal["informational_flags"] = existing

    if new_proposal is not None:
        out["proposal"] = new_proposal

    return out


def _option_details_from_proposal(proposal: dict[str, Any], *, symbol: str) -> OptionLegDetails:
    """Rebuild the option leg the Drafter priced, from the persisted
    proposal dict. Every field an options risk rule needs (premium inputs,
    multiplier, liquidity/IV/earnings snapshot) must already be sitting in
    the proposal by the time it gets here — the Drafter writes them all at
    draft-time, since this node (like the executor's re-risk-check) only
    ever reads the persisted proposal, never live state.
    """
    expiry_raw = proposal.get("expiry_date")
    expiry = date.fromisoformat(str(expiry_raw)) if expiry_raw else date.today()
    contract_type = cast(
        Literal["call", "put"], proposal.get("contract_type") or "call"
    )
    action = cast(
        Literal["buy_to_open", "sell_to_close"],
        proposal.get("option_action") or "buy_to_open",
    )
    return OptionLegDetails(
        underlying_symbol=symbol,
        occ_symbol=str(proposal.get("occ_symbol", "")),
        contract_type=contract_type,
        strike=float(proposal.get("strike", 0.0)),
        expiry=expiry,
        multiplier=int(proposal.get("multiplier", 100)),
        action=action,
        open_interest=_opt_int(proposal.get("open_interest")),
        volume=_opt_int(proposal.get("volume")),
        bid=_opt_float(proposal.get("bid")),
        ask=_opt_float(proposal.get("ask")),
        implied_volatility=_opt_float(proposal.get("implied_volatility")),
        days_to_earnings=_opt_int(proposal.get("days_to_earnings")),
    )


def _opt_float(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _opt_int(v: object) -> int | None:
    try:
        return int(v)  # type: ignore[call-overload, no-any-return]
    except (TypeError, ValueError):
        return None


def _opt_bool(v: object) -> bool | None:
    """Preserve the tri-state. ``None`` must NOT collapse to False here —
    ``shortable_check`` distinguishes "broker says no" from "we never
    asked", and flattening them would erase that in the audit log."""
    if v is None:
        return None
    return bool(v)


def _default_provider(state: CouncilState) -> RiskContextProvider:
    """Pick the provider. ``USE_POSTGRES=1`` → PostgresRiskContextProvider
    reading reconciler-written snapshots; otherwise Mock (synthetic context
    from the feature dict). Same env switch as ``app.services.store``.
    """

    if env_flag("USE_POSTGRES"):
        # Lazy import — keeps the agents package light when running offline.
        from engine.db.session import async_session_factory
        from engine.risk import PostgresRiskContextProvider

        return PostgresRiskContextProvider(session_factory=async_session_factory())

    ctx = state.get("context") or {}
    # `x or default` treats a genuinely-zero equity the same as an absent
    # one (0.0 is falsy) — same bug as drafter.py's sizing inputs. Only a
    # missing key gets the fixture; a present zero is real.
    raw_equity = ctx.get("portfolio_equity")
    equity = float(raw_equity) if raw_equity is not None else 100_000.0
    return MockRiskContextProvider(
        account_equity=equity,
        cash=equity,
        buying_power=equity * 2.0,
    )
