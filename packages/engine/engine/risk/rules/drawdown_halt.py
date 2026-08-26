"""drawdown_halt — non-negotiable v1 rule.

If the user's circuit breaker is already halted (drawdown threshold breached
on a prior tick), block every proposal that is a NEW BET. Exits — a SELL
that only closes/reduces a long, or a BUY that only covers/reduces a short —
are allowed so the user can flatten. Halt persists until the user explicitly
acknowledges — no automatic un-halt on a new trading day.

If the breaker is NOT halted but today's P&L has just crossed the threshold,
flip the breaker AND block this proposal. Subsequent ticks see the persisted
halt state.

Symmetric by construction, not by side:
  - A SELL that opens/extends a short IS a new bet (the short-side mirror
    of a BUY that opens/extends a long) — it stays gated. Exempting every
    SELL unconditionally, as this rule used to, let a short open DURING an
    active halt.
  - A BUY that only covers/reduces an existing short is de-risking, same
    as a SELL that only closes/reduces a long — it is exempt too. Blocking
    every BUY unconditionally would have stopped a user from de-risking a
    short during a halt, which contradicts the entire reason SELL-to-close
    is exempted at all.
  - A BUY that covers a short AND crosses into a new long (qty exceeds the
    held short) is only PARTLY a close — ``covers_short_only`` is False the
    moment any part of the order goes net-long, so that case stays gated.

veto_rule names:
    drawdown_halt_active        already halted from prior tick
    drawdown_halt_just_tripped  this evaluation triggered the trip
"""

from __future__ import annotations

from engine.risk.rules._short import covers_short_only, opens_short
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def drawdown_halt(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    # A SELL that does NOT open/extend a short is a pure close — always
    # allowed so the user can flatten. (A SELL that DOES open/extend a
    # short falls through: it's a new bet, gated like any other.)
    if proposal.side is Side.SELL and not opens_short(proposal, context):
        return None

    # Symmetrically: a BUY that only covers/reduces an existing short is
    # also pure de-risking, not a new bet — exempt it too.
    if covers_short_only(proposal, context):
        return None

    if context.drawdown_halted:
        reason = context.drawdown_halt_reason or (
            f"Account previously halted at "
            f"{context.daily_pnl_pct:.2f}% — awaiting user acknowledgement."
        )
        return RiskDecision(
            approved=False,
            reason=reason,
            veto_rule="drawdown_halt_active",
        )

    if context.daily_pnl_pct <= caps.daily_drawdown_halt_pct:
        return RiskDecision(
            approved=False,
            reason=(
                f"Daily drawdown {context.daily_pnl_pct:.2f}% breached halt "
                f"threshold {caps.daily_drawdown_halt_pct:.2f}%. Agent halted; "
                "user must acknowledge to resume."
            ),
            veto_rule="drawdown_halt_just_tripped",
        )

    return None
