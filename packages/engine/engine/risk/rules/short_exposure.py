"""short_unbounded_loss_cap — a tighter notional ceiling for SHORT entries.

A long and a short of the same dollar size are not the same risk, and
sizing them the same way is the mistake this rule exists to prevent.

  - A long's loss is bounded at the notional. The stock goes to zero and
    the position, having shrunk the whole way down, is worth nothing.
  - A short's loss is unbounded and CONVEX. The position grows against you
    as it moves: a name that doubles turns a 5%-of-equity short into a
    10%-of-equity liability, and it keeps compounding from there.

So the cap is derived from the adverse move a stop cannot defend — an
overnight gap or a halt reopen, where the resting stop becomes a market
order at whatever the book will pay. ``RiskCaps.max_short_position_pct``
carries the full arithmetic; the short version is that a +150% gap
scenario forces a ~2% notional cap to bound the loss at the same -5% of
equity a maxed-out long can produce.

Two ceilings, one rule, because they are the same question at two scales:

  1. per-position — trims to ``max_short_position_pct`` (TRIM, not reject:
     a smaller short still expresses the thesis).
  2. book-wide    — vetoes past ``max_short_gross_pct`` of total short
     notional. Five uncorrelated-looking shorts squeeze together on the
     same risk-on day; the aggregate is the number that matters.

Only applies to SELL-TO-OPEN. A SELL that closes a held long reduces risk
and is never trimmed by this rule.

veto_rule (per-position trim): short_unbounded_loss_cap_trim
veto_rule (per-position block): short_unbounded_loss_cap
veto_rule (book-wide block):    short_gross_exposure_cap
"""

from __future__ import annotations

from engine.risk.rules._short import opens_short
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def short_unbounded_loss_cap(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if not opens_short(proposal, context):
        return None
    if context.account_equity <= 0:
        return RiskDecision(
            approved=False,
            reason="Account equity is non-positive; refusing any new SHORT.",
            veto_rule="short_unbounded_loss_cap",
        )

    qty = proposal.qty
    notional = qty * proposal.last_price
    pct = (notional / context.account_equity) * 100.0

    if pct > caps.max_short_position_pct:
        adjusted = int(
            (caps.max_short_position_pct / 100.0)
            * context.account_equity
            / proposal.last_price
        )
        if adjusted < caps.min_qty:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Short would be {pct:.1f}% of equity (short cap "
                    f"{caps.max_short_position_pct:.1f}% — tighter than the "
                    f"{caps.max_position_pct:.1f}% long cap because a short's "
                    "loss is unbounded); trimming rounds to "
                    f"{adjusted} share(s) < min_qty {caps.min_qty}."
                ),
                veto_rule="short_unbounded_loss_cap",
            )
        qty = adjusted

    # Book-wide gross short exposure, measured on the POST-TRIM qty.
    existing_short = sum(
        abs(p.market_value)
        for p in context.open_positions
        if p.qty < 0 and p.symbol != proposal.symbol
    )
    gross = existing_short + qty * proposal.last_price
    gross_pct = (gross / context.account_equity) * 100.0
    if gross_pct > caps.max_short_gross_pct:
        return RiskDecision(
            approved=False,
            reason=(
                f"Total short exposure would reach {gross_pct:.1f}% of equity "
                f"(cap {caps.max_short_gross_pct:.1f}%). Shorts squeeze together; "
                "the book-wide number is the one that ends accounts."
            ),
            veto_rule="short_gross_exposure_cap",
        )

    if qty != proposal.qty:
        return RiskDecision(
            approved=True,
            reason=(
                f"Trimmed short {proposal.qty} → {qty} share(s) "
                f"({pct:.1f}% → {caps.max_short_position_pct:.1f}% of equity; "
                "shorts carry unbounded downside so they cap tighter than longs)."
            ),
            adjusted_qty=qty,
            veto_rule="short_unbounded_loss_cap_trim",
        )
    return None
