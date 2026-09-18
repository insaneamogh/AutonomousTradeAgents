"""Refuse a contract that expires before its own thesis can resolve.

The largest defect measured in this system: **the signals predict months
and the positions were held 2.1 days.**

    _momentum        ret_252d_pct (12mo) + ret_63d_pct (3mo)  -> 60 trading days
    _sma_crossover   20/50-day moving averages                -> 20 trading days
                                                                 |
                                       measured average hold:  2.1 days

Buying a 30-day option on a signal whose own window is three months is a bet
on *timing*, not on the thesis: the contract expires, or its premium decays
past the stop, long before the thing it was predicting has had a chance to
happen. Every loss in the recorded book has that shape.

This rule does not try to fix the mismatch — it refuses the trade and names
it, so the mismatch is visible in the Refusal Ledger instead of being paid
for one contract at a time. There are exactly two honest resolutions and
both belong to the operator, not to a default:

  - buy a longer-dated contract (a 60-trading-day thesis needs ~84 calendar
    days, and `options_max_dte` is currently 60 — so under today's caps a
    `momentum` thesis has NO tradeable contract, which is worth knowing);
  - or trade that thesis in equity, where a multi-week hold costs nothing
    in theta.

Self-gates to a pass whenever it cannot assess: no strategy, no expiry, or
a non-entry action. An unattributed proposal is not refused here — it is
refused by name elsewhere if at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def horizon_exceeds_contract(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    if not proposal.strategy_id:
        # Unattributed: cannot assess the thesis, so do not pretend to.
        return None

    # Imported here so `packages/engine` keeps no import-time dependency on
    # `apps/agents`, which owns the strategy definitions.
    try:
        from trading_agents.strategies.horizon import horizon_calendar_days
    except ImportError:  # pragma: no cover — engine used without the agents pkg
        return None

    needed = horizon_calendar_days(proposal.strategy_id)
    now = context.now_utc or datetime.now(UTC)
    dte = (option.expiry - now.date()).days
    if dte >= needed:
        return None

    return RiskDecision(
        approved=False,
        reason=(
            f"A {proposal.strategy_id} thesis needs about {needed} calendar "
            f"days to resolve and this contract has {dte}. The option expires "
            "before the signal it was bought on can play out — that is a bet "
            "on timing, not on the thesis. Use a longer-dated contract, or "
            "trade this thesis in equity where holding costs no theta."
        ),
        veto_rule="horizon_exceeds_contract",
    )
