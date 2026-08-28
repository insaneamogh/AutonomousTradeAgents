"""earnings_blackout — no new entry within N days of the underlying's
next earnings.

Entry-only. IV typically crushes around a known earnings event; buying a
long option into that window is a different trade from the directional
thesis the council proposed. Self-gates (returns ``None``, does NOT
block) when ``days_to_earnings`` is ``None`` — missing earnings-calendar
data must not halt trading, matching this rule set's broader "missing
data doesn't halt" principle (e.g. ``engine.risk.rules.sector_concentration``
on an unknown sector).

veto_rule: earnings_blackout
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def earnings_blackout(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    days = option.days_to_earnings
    if days is None:
        return None
    if abs(days) > caps.options_earnings_blackout_days:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"{option.underlying_symbol} reports earnings in {days} "
            f"day(s) — inside the {caps.options_earnings_blackout_days}-day "
            "blackout window (IV crush risk)."
        ),
        veto_rule="earnings_blackout",
    )
