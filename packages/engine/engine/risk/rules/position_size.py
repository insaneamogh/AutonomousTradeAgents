"""position_size_cap — trim or block based on % of equity.

If a proposal exceeds the per-position cap, we don't reject outright — we
trim it to the cap and return an ``adjusted_qty``. If trimming rounds to 0
shares, we reject.

veto_rule (when blocking):  max_position_pct
veto_rule (when trimming):  max_position_pct_trim
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def position_size_cap(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.side is not Side.BUY:
        return None
    if context.account_equity <= 0:
        return RiskDecision(
            approved=False,
            reason="Account equity is non-positive; refusing any new BUY.",
            veto_rule="max_position_pct",
        )

    notional = proposal.qty * proposal.last_price
    pct = (notional / context.account_equity) * 100.0
    if pct <= caps.max_position_pct:
        return None

    # Trim — keep the trade but cap the size.
    adjusted = int((caps.max_position_pct / 100.0) * context.account_equity / proposal.last_price)
    if adjusted < caps.min_qty:
        return RiskDecision(
            approved=False,
            reason=(
                f"Position would be {pct:.1f}% of equity (cap "
                f"{caps.max_position_pct:.1f}%); trimming rounds to "
                f"{adjusted} share(s) < min_qty {caps.min_qty}."
            ),
            veto_rule="max_position_pct",
        )
    return RiskDecision(
        approved=True,
        reason=(
            f"Trimmed {proposal.qty} → {adjusted} share(s) "
            f"({pct:.1f}% → {caps.max_position_pct:.1f}% of equity)."
        ),
        adjusted_qty=adjusted,
        veto_rule="max_position_pct_trim",
    )
