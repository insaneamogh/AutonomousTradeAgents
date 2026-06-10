"""drawdown_halt — non-negotiable v1 rule.

If the user's circuit breaker is already halted (drawdown threshold breached
on a prior tick), block ALL new BUYs. Sells / exits are allowed so the user
can flatten. Halt persists until the user explicitly acknowledges — no
automatic un-halt on a new trading day.

If the breaker is NOT halted but today's P&L has just crossed the threshold,
flip the breaker AND block this proposal. Subsequent ticks see the persisted
halt state.

veto_rule names:
    drawdown_halt_active        already halted from prior tick
    drawdown_halt_just_tripped  this evaluation triggered the trip
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def drawdown_halt(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    # Sells are always allowed so the user can flatten.
    if proposal.side is Side.SELL:
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
