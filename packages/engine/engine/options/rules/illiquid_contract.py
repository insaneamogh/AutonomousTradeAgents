"""illiquid_contract — liquidity floor, re-checked at the risk gate.

Entry-only — a close must always be possible however illiquid the
contract has become; trapping a user in a position because the market
thinned out would be strictly worse than letting them exit at a bad
price. Independent of any upstream selection-time filtering (the
separately-built ``engine.options.selection`` chain-scan algorithm) — a
contract liquid when selected can be stale by approval time, which is
exactly why this rule exists here too, not just there. On a
15-minute-delayed indicative feed (docs/OPTIONS_PLAN.md §0) this is one
of the most important checks in the whole rule set: a wide book shows a
fill price that may never actually be available.

Three independent conditions, any one of which vetoes:
  - open interest missing or below ``caps.options_min_open_interest``
  - volume missing or below ``caps.options_min_volume``
  - bid AND ask both present, and the relative spread
    ``(ask-bid)/((ask+bid)/2) * 100`` exceeds
    ``caps.options_max_relative_spread_pct``

veto_rule: illiquid_contract
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def illiquid_contract(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None

    oi = option.open_interest
    if oi is None or oi < caps.options_min_open_interest:
        return RiskDecision(
            approved=False,
            reason=(
                f"{option.occ_symbol} open interest {oi!r} is below the "
                f"{caps.options_min_open_interest} floor."
            ),
            veto_rule="illiquid_contract",
        )

    # ``> 0`` guard mirrors ``selection._passes_liquidity``: the floor must be
    # switchable off, and without this a floor of 0 would still hard-fail on a
    # None volume. See that function for why this figure is a last-trade-size
    # proxy rather than real daily volume, and why open interest above carries
    # the actual liquidity judgment.
    vol = option.volume
    if caps.options_min_volume > 0 and (vol is None or vol < caps.options_min_volume):
        return RiskDecision(
            approved=False,
            reason=(
                f"{option.occ_symbol} volume {vol!r} is below the "
                f"{caps.options_min_volume} floor."
            ),
            veto_rule="illiquid_contract",
        )

    if option.bid is not None and option.ask is not None:
        mid = (option.bid + option.ask) / 2.0
        if mid > 0:
            spread_pct = (option.ask - option.bid) / mid * 100.0
            if spread_pct > caps.options_max_relative_spread_pct:
                return RiskDecision(
                    approved=False,
                    reason=(
                        f"{option.occ_symbol} relative spread "
                        f"{spread_pct:.1f}% exceeds the "
                        f"{caps.options_max_relative_spread_pct:.1f}% cap — "
                        "on a 15-minute-delayed feed a wide book is a fill "
                        "price that may never exist."
                    ),
                    veto_rule="illiquid_contract",
                )

    return None
