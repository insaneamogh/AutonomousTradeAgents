"""wash_sale — IRS wash-sale INFORMATIONAL warning.

IRS rule: a "wash sale" occurs when you sell a security at a loss and buy
the same (or substantially identical) security within 30 days. The loss
is disallowed for the current tax year and added to the cost basis of the
replacement position.

This rule is INFORMATIONAL ONLY. It never vetoes. It returns
``approved=True`` with ``informational_flags=('wash_sale_warning',)`` so
the UI / audit log can surface a chip on the ApprovalCard. The user can
still proceed if they want to — IRS doesn't prevent the trade, just
defers the deduction.

Phase 0/1 simplifications (called out, not hidden):
  - Lookback uses calendar days. Phase 1.5 swaps to NY business days via
    ``pandas_market_calendars``.
  - "Substantially identical" check is exact symbol match. Real IRS rule
    extends to options, ETFs covering the same index, etc. — explicit
    deferral, not a stealth bug.

veto_rule (informational): wash_sale_warning
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.risk.markets import market_of
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def wash_sale(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    # IRS tax rule — US securities only. (India's grandfathering/STT rules
    # are entirely different and out of scope here.)
    if market_of(proposal.symbol) != "US":
        return None

    if proposal.side is not Side.BUY:
        return None
    if not context.recent_losing_closes:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(days=caps.wash_sale_lookback_days)).date()
    matching = [
        c for c in context.recent_losing_closes
        if c.symbol == proposal.symbol
        and c.closed_at >= cutoff
        and c.realized_pnl < 0
    ]
    if not matching:
        return None

    most_recent = max(matching, key=lambda c: c.closed_at)
    return RiskDecision(
        approved=True,  # INFORMATIONAL — never blocks
        reason=(
            f"Wash-sale risk: {proposal.symbol} was closed at a loss on "
            f"{most_recent.closed_at} (${most_recent.realized_pnl:.2f}). "
            f"Re-entering within {caps.wash_sale_lookback_days} days defers "
            "the loss for IRS tax purposes."
        ),
        informational_flags=("wash_sale_warning",),
    )
