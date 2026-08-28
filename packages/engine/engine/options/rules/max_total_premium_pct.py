"""max_total_premium_pct — portfolio-aggregate long-option premium cap.

Entry-only, evaluated post-trim (the engine runs this AFTER
``max_premium_pct``, the same "trim first, then check the aggregate"
ordering the equity engine uses for ``position_size_cap`` before
``sector_concentration``/``single_name_concentration``). Sums the market
value of every currently-held OPTION position — already correctly
multiplier-scaled at the source, per ``PortfolioPosition.market_value``'s
existing contract — plus this proposal's own premium, as a % of equity.

This is deliberately what SUBSTITUTES for portfolio greek caps in Phase A.
``portfolio_delta_cap``/``portfolio_theta_cap`` (docs/OPTIONS_PLAN.md §2.5)
are deferred to Phase B/C, not built here, because Phase A structures are
long-only, single-leg, and bounded-loss by construction: the worst case
the WHOLE options book can produce is losing 100% of the premium paid,
which this one number already caps. Non-bounded structures (short legs,
multi-leg spreads with a different max-loss shape) would make that
reasoning insufficient — exactly why Phase B/C need real greek caps and
Phase A does not.

veto_rule: max_total_premium_pct
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def max_total_premium_pct(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    if context.account_equity <= 0:
        return RiskDecision(
            approved=False,
            reason="Account equity is non-positive; refusing any new options BUY.",
            veto_rule="max_total_premium_pct",
        )

    existing = sum(p.market_value for p in context.open_positions if p.is_option)
    this_premium = proposal.qty * proposal.last_price * option.multiplier
    total = existing + this_premium
    pct = (total / context.account_equity) * 100.0
    if pct <= caps.options_max_total_premium_pct:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"Total open-option premium would reach {pct:.2f}% of equity "
            f"(cap {caps.options_max_total_premium_pct:.2f}%). Phase A's "
            "long-only/bounded-loss structures mean this one number is "
            "already the whole book's worst case."
        ),
        veto_rule="max_total_premium_pct",
    )
