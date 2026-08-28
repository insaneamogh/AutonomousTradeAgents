"""expiry_day_entry — no new options position expiring today.

A new long entered on its own expiry day has essentially no time left to
work and sits at the highest-gamma, worst-execution moment on a
15-minute-delayed indicative feed (docs/OPTIONS_PLAN.md §0/§2.2).
Entry-only — never blocks a same-day close, which must always be
possible, expiry day most of all.

veto_rule: expiry_day_entry
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.options.expiry import is_expiry_day
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def expiry_day_entry(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    now = context.now_utc or datetime.now(UTC)
    if not is_expiry_day(option.expiry, now):
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"{option.occ_symbol} expires today — no new entries on a "
            "contract's own expiry day."
        ),
        veto_rule="expiry_day_entry",
    )
