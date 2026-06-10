"""derivative_notional_cap — cap a single India derivative order's notional.

Derivatives are margin-traded: the cash debited (SPAN+exposure margin) is a
fraction of the contract notional, so the plain position-size cap — which
reasons in market value — understates true exposure. This rule checks the
CONTRACT NOTIONAL (qty × last_price) against a % of account equity.

Runs post-trim in the engine, so the notional reflects any position-size
trim already applied. No trim here: derivative quantities move in lots, so
a partial trim would immediately violate lot_size_block — veto with the
largest valid sizing in the message instead.

veto_rule: derivative_notional_cap
"""

from __future__ import annotations

from engine.risk.markets import is_derivative
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def derivative_notional_cap(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if not is_derivative(proposal.symbol):
        return None
    if context.account_equity <= 0:
        return RiskDecision(
            approved=False,
            reason="Account equity is zero or unknown — refusing derivative order.",
            veto_rule="derivative_notional_cap",
        )

    notional = proposal.qty * proposal.last_price
    cap_notional = context.account_equity * (caps.max_derivative_notional_pct / 100.0)
    if notional > cap_notional:
        return RiskDecision(
            approved=False,
            reason=(
                f"Derivative notional {notional:,.0f} exceeds "
                f"{caps.max_derivative_notional_pct:.0f}% of equity "
                f"({cap_notional:,.0f}). Reduce lots."
            ),
            veto_rule="derivative_notional_cap",
        )
    return None
