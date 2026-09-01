"""``open_option_trade`` / ``adjust_option_position`` — the two mutating
tool handlers ``tools.guard.ToolGuard.before()`` clears before either one
runs.

Every deterministic decision — which contract, how many, whether risk
approves — is ALREADY MADE by the time either function is called. Both
trust ``guard_payload`` (built by ``ToolGuard.before()``/``_before_scale_in``)
rather than re-deriving anything: no ``select_contract``, no
``options_position_size``, no ``engine.risk.evaluate`` call lives in this
file. That split keeps ``guard.py`` independently testable against the
12-step stack (docs/IMPL_OPTIONS_AGENTS.md §2.1) and keeps these two
functions honest "place the order, write the row" executors with nothing
left to get wrong about risk (CLAUDE.md §3: "agents propose, deterministic
code disposes" — the guard disposes; this module only ever EXECUTES a
disposal already made).

Handler signature is ``(args, ctx, guard_payload) -> dict`` — see
``guard.dispatch_tool_call`` for why the third argument exists (the guard
resolves ALL infrastructure once and hands it down, so nothing here
re-resolves a broker client, a decision log, or a DB session factory on
its own).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from broker.types import OrderRequest, OrderType, TimeInForce
from broker.types import Side as BrokerSide
from trading_agents.memory.decision_log import DecisionEntry
from trading_agents.options.tools.guard import (
    GuardContext,
    persist_option_state,
    persist_placed_order,
    stamp_position_closed,
)

logger = logging.getLogger("agents.options.trade")

__all__ = ["adjust_option_position", "open_option_trade"]


def _client_order_id(prefix: str, key: str) -> str:
    # Alpaca's max is 128 chars; every id we construct fits comfortably,
    # but truncate defensively rather than let a long decision_id ever
    # produce a rejected order.
    return f"agent-{prefix}-{key}"[:128]


def _proposal_dto(
    *,
    underlying: str,
    direction: str,
    option: Any,
    qty: int,
    limit_price: float,
    conviction: float,
    thesis: str,
) -> dict[str, Any]:
    """A camelCase dict mirroring ``ApprovalProposalDto``'s wire shape
    (``apps/api/app/schemas/approvals.py``) field-for-field — this is what
    ``position_manager.py`` (``stored_proposal.get("occSymbol")``,
    ``.get("isOption")``, …) and ``funnel_service.py`` already read off
    ``agent_decisions.proposal``. Built by hand, not imported from
    ``apps.api``: ``apps/agents/pyproject.toml`` does not depend on
    ``apps/api`` (see ``guard.py``'s ``_trading_mode`` docstring for the
    same boundary), so a new options entry created by this tool must LOOK,
    in the database, exactly like one the existing human-approval pipeline
    would have written — same keys, same casing — or the ratchet, the
    ghost-eval marker and the funnel UI would silently mis-read it.
    """
    return {
        "id": str(uuid.uuid4()),
        "symbol": underlying,
        "side": "BUY",
        "direction": direction,
        "isOption": True,
        "optionAction": "buy_to_open",
        "occSymbol": option.occ_symbol,
        "strike": option.strike,
        "expiryDate": option.expiry.isoformat(),
        "contractType": option.contract_type,
        "multiplier": option.multiplier,
        "openInterest": option.open_interest,
        "volume": option.volume,
        "bid": option.bid,
        "ask": option.ask,
        "impliedVolatility": option.implied_volatility,
        "daysToEarnings": option.days_to_earnings,
        "qty": qty,
        "orderType": "LIMIT",
        "limitPrice": round(limit_price, 2),
        "estimatedNotional": round(qty * limit_price * option.multiplier, 2),
        "rationale": thesis,
        "bullCase": thesis if direction == "long" else "",
        "bearCase": thesis if direction == "short" else "",
        "convictionLevel": conviction,
        # Same key the equity path writes (runtime._to_proposal_dto). For
        # the options fork the resolved bull/bear conviction IS the 0-1
        # confidence the guard already scored against the floor, so
        # persisting it here makes the executor's re-check re-evaluate the
        # SAME number rather than fall back to a conviction/5 stand-in.
        "councilConfidence": conviction,
    }


async def open_option_trade(
    args: dict[str, Any], ctx: GuardContext, guard_payload: dict[str, Any]
) -> dict[str, Any]:
    """Runs only after ``ToolGuard.before()`` has cleared all 12 steps.

    Places the LIMIT buy_to_open at the guard-selected contract/qty/price
    (never a market order — a 15-minute-delayed indicative feed makes a
    market order an invitation to be filled at a price nobody quoted, per
    docs/OPTIONS_PLAYBOOK.md §1.5), then persists the ``agent_decisions``
    row that anchors this trade in the audit trail — the SAME row whose id
    ``adjust_option_position`` later refers back to via ``decision_id``.

    Reuses ``ctx.council_run_id`` as that row's primary key, mirroring
    ``PostgresDecisionLog.record()``'s own existing convention ("entry.id
    is council_run_id when runtime.run_council built this entry") rather
    than inventing a second id-assignment scheme for this one entry point.
    """
    option = guard_payload["option"]
    qty = int(guard_payload["qty"])
    limit_price = float(guard_payload["limit_price"])
    broker_factory = guard_payload["broker_factory"]
    decision_log = guard_payload["decision_log"]
    now: datetime = guard_payload.get("now") or datetime.now(UTC)
    decision = guard_payload["risk_decision"]

    broker = broker_factory()
    client_order_id = _client_order_id("open", str(ctx.council_run_id))
    order = await broker.place_order(
        OrderRequest(
            symbol=option.occ_symbol,
            side=BrokerSide.BUY_TO_OPEN,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=round(limit_price, 2),
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
    )

    direction = guard_payload["direction"]
    thesis = guard_payload["thesis"]
    proposal_dto = _proposal_dto(
        underlying=guard_payload["underlying"],
        direction=direction,
        option=option,
        qty=qty,
        limit_price=limit_price,
        conviction=guard_payload["conviction"],
        thesis=thesis,
    )

    entry = DecisionEntry(
        id=str(ctx.council_run_id),
        user_id=ctx.user_id,
        symbol=guard_payload["underlying"],
        horizon="short",
        final_action="BUY",
        risk_approved=True,
        risk_veto_rule=None,
        risk_reason=decision.reason,
        bull_case=thesis if direction == "long" else None,
        bear_case=thesis if direction == "short" else None,
        proposal_dto=proposal_dto,
        # Honest pass-through of what the broker actually reported — an
        # order accepted-but-not-yet-filled must NOT read as filled. That
        # also means it will not (yet) satisfy manage_positions_for_user's
        # `fill_qty IS NOT NULL` predicate, which is the correct behavior:
        # an unfilled order is not a position the ratchet should manage.
        fill_qty=order.filled_qty or None,
        fill_avg_price=order.avg_fill_price,
        completed_at=now,
        reasoning={
            "option_exit": {
                "stop_loss_pct": guard_payload["stop_loss_pct"],
                "take_profit_pct": guard_payload["take_profit_pct"],
                "adds_this_position": 0,
            },
            "risk_checks_passed": list(decision.checks_passed),
            # `ToolGuard._before_open_option_trade` already ran
            # select_contract and shaped this via `engine.options.
            # funnel_block` — carried straight through rather than
            # re-derived, so "we looked at N contracts and bought this
            # one" is on the row a successful open writes too, not just
            # a HOLD's. Absent only if guard_payload somehow lacks it
            # (defensive default; every real caller sets it).
            "contract_funnel": guard_payload.get("contract_funnel"),
        },
    )
    saved = await decision_log.record(entry)

    # The ONE audit-chain write this function used to skip entirely — see
    # persist_placed_order's own docstring for why that silently disabled
    # every fill_qty-gated exit mechanism (the ratchet, the mandatory
    # expiry sweep) for every position opened through this tool.
    await persist_placed_order(
        guard_payload.get("session_factory"),
        user_id=ctx.user_id,
        decision_id=saved.id,
        client_order_id=client_order_id,
        underlying=guard_payload["underlying"],
        order=order,
        option_action="buy_to_open",
        multiplier=option.multiplier,
    )

    return {
        "decision_id": saved.id,
        "user_id": ctx.user_id,
        "occ_symbol": option.occ_symbol,
        "underlying": guard_payload["underlying"],
        "qty": qty,
        "limit_price": limit_price,
        "order_id": order.broker_order_id,
        "order_status": getattr(order.status, "value", str(order.status)),
        "checks_passed": list(decision.checks_passed),
    }


async def adjust_option_position(
    args: dict[str, Any], ctx: GuardContext, guard_payload: dict[str, Any]
) -> dict[str, Any]:
    """Executes whichever ``action`` ``ToolGuard.before()`` already
    validated against the ratchet invariant. This function never decides
    whether an adjustment is allowed — it only ever runs after the guard
    already said yes."""
    action = guard_payload["action"]
    decision_id = guard_payload["decision_id"]
    session_factory = guard_payload.get("session_factory")

    if action == "HOLD":
        return {
            "decision_id": decision_id,
            "user_id": ctx.user_id,
            "action": action,
            "changed": False,
        }

    if action == "EXIT_NOW":
        return await _exit_now(guard_payload, ctx)

    if action in ("TIGHTEN_STOP", "RAISE_TAKE_PROFIT"):
        await persist_option_state(
            session_factory,
            decision_id=decision_id,
            state=guard_payload["option_state"],
        )
        return {
            "decision_id": decision_id,
            "user_id": ctx.user_id,
            "action": action,
            "changed": True,
            "value": guard_payload["value"],
        }

    if action == "SCALE_IN":
        return await _scale_in(guard_payload, ctx)

    return {  # pragma: no cover — guard.before() never allows an unknown action
        "decision_id": decision_id,
        "user_id": ctx.user_id,
        "action": action,
        "changed": False,
    }


async def _exit_now(guard_payload: dict[str, Any], ctx: GuardContext) -> dict[str, Any]:
    decision_id = guard_payload["decision_id"]
    occ_symbol = guard_payload["occ_symbol"]
    broker = guard_payload["broker_factory"]()
    now: datetime = guard_payload.get("now") or datetime.now(UTC)

    position = await broker.get_position(occ_symbol) if occ_symbol else None
    if position is None or position.qty == 0:
        return {
            "decision_id": decision_id,
            "user_id": ctx.user_id,
            "action": "EXIT_NOW",
            "changed": False,
            "note": "no open broker position for this contract",
        }

    await broker.cancel_open_orders(occ_symbol)

    multiplier = position.multiplier or 100
    # Current mark, not the entry price — mirrors position_manager.py's
    # own `held.market_value / (held.qty * multiplier)` computation for
    # exactly the same reason: a LIMIT close priced at the ENTRY would be
    # a stale, meaningless number for a position that has since moved.
    current_mark = (
        abs(position.market_value / (position.qty * multiplier))
        if position.qty
        else None
    )
    limit_price = current_mark if current_mark and current_mark > 0 else position.avg_entry_price

    client_order_id = _client_order_id("exit", str(decision_id))
    order = await broker.place_order(
        OrderRequest(
            symbol=occ_symbol,
            side=BrokerSide.SELL_TO_CLOSE,
            qty=abs(position.qty),
            order_type=OrderType.LIMIT,
            limit_price=round(limit_price, 2) if limit_price else None,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
    )

    # Same audit-chain write open_option_trade now does on entry — an exit
    # this tool places needs an orders row exactly as much as an open does,
    # or order_sync.py has nothing to converge the close's fill/closed_at
    # against either.
    await persist_placed_order(
        guard_payload.get("session_factory"),
        user_id=ctx.user_id,
        decision_id=decision_id,
        client_order_id=client_order_id,
        underlying=guard_payload["underlying"],
        order=order,
        option_action="sell_to_close",
        multiplier=multiplier,
    )

    await stamp_position_closed(
        guard_payload.get("session_factory"),
        decision_id=decision_id,
        user_id=ctx.user_id,
        reason="agent_signal",
        now=now,
    )

    return {
        "decision_id": decision_id,
        "user_id": ctx.user_id,
        "action": "EXIT_NOW",
        "changed": True,
        "order_id": order.broker_order_id,
        "qty": abs(position.qty),
    }


async def _scale_in(guard_payload: dict[str, Any], ctx: GuardContext) -> dict[str, Any]:
    decision_id = guard_payload["decision_id"]
    occ_symbol = guard_payload["occ_symbol"]
    qty = int(guard_payload["qty"])
    limit_price = float(guard_payload["limit_price"])
    option_state = guard_payload["option_state"]
    session_factory = guard_payload.get("session_factory")

    broker = guard_payload["broker_factory"]()
    client_order_id = _client_order_id(
        f"add{option_state.get('adds_this_position', 0)}", str(decision_id)
    )
    order = await broker.place_order(
        OrderRequest(
            symbol=occ_symbol,
            side=BrokerSide.BUY_TO_OPEN,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=round(limit_price, 2),
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
    )

    # Same reasoning as open_option_trade/_exit_now: a scale-in is a real
    # second fill against the same position and needs its own orders row.
    await persist_placed_order(
        session_factory,
        user_id=ctx.user_id,
        decision_id=decision_id,
        client_order_id=client_order_id,
        underlying=guard_payload["underlying"],
        order=order,
        option_action="buy_to_open",
        multiplier=100,
    )

    await persist_option_state(session_factory, decision_id=decision_id, state=option_state)

    return {
        "decision_id": decision_id,
        "user_id": ctx.user_id,
        "action": "SCALE_IN",
        "changed": True,
        "qty": qty,
        "limit_price": limit_price,
        "order_id": order.broker_order_id,
        "adds_this_position": option_state.get("adds_this_position"),
    }
