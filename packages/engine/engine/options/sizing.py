"""Premium-at-risk sizing — replaces ATR entirely on the options path.

``docs/OPTIONS_PLAN.md`` §2.4: a long option's entire premium IS the max
loss (there is no meaningful ATR stop distance for an instrument whose risk
is bounded by what you paid for it, not by how far the underlying moves).
So sizing is pure floor division of a dollar budget by the contract's cost:

    qty = floor(budget_usd / (ask * multiplier))

``qty`` NEVER rounds up — a budget that can't afford one contract sizes to
zero, exactly like ``engine.sizing.atr_position_size``'s qty<1 branch
(see ``packages/engine/engine/sizing/atr.py``): a ``SizingDecision``-shaped
result with ``qty=0`` and a ``.notes`` string explaining why, for the
caller (the Drafter) to convert into a named HOLD — never a smaller trade
than the floor allows and never an exception.

This module deliberately does NOT enforce the portfolio-aggregate cap
(``RiskCaps.options_max_total_premium_pct`` — all open long premium
combined). That is the risk engine's job, mirroring how
``RiskCaps.max_short_gross_pct`` / ``short_unbounded_loss_cap`` works today
for the equity short book (see
``packages/engine/engine/risk/rules/short_exposure.py``): a single-position
sizer computes what ONE trade would cost, and a separate, independent
book-wide rule re-derives and re-validates the aggregate against the
CURRENT portfolio snapshot at risk-gate time — not against whatever this
function assumed when it ran. This function's ``budget_usd`` input is
expected to already be the single-position budget (typically
``account_equity * caps.options_max_premium_pct / 100`` — computed by the
caller, not here), and its output is never trusted un-vetoed by the risk
gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OptionsSizingInputs:
    budget_usd: float
    """Single-position premium budget in dollars — e.g.
    ``account_equity * caps.options_max_premium_pct / 100``. NOT the
    portfolio-aggregate cap; see module docstring."""
    ask: float
    """The contract's ask price, per share of the underlying (i.e. before
    the multiplier) — the same convention ``ContractQuote.ask`` uses."""
    multiplier: int
    """100 for standard US equity options. Total cost of one contract is
    ``ask * multiplier``."""

    open_interest: int | None = None
    """The CONTRACT'S own open interest, for the liquidity trim below.
    ``None`` (the default) skips the trim entirely, so every existing
    caller and test keeps its exact previous behaviour — the trim is
    opt-in by passing real data, never by silently assuming a number."""

    max_pct_of_open_interest: float = 0.0
    """``RiskCaps.options_max_pct_of_open_interest``. 0 disables the trim,
    matching how the other options caps document "0 turns this side off"."""

    conviction: float | None = None
    """The council's resolved conviction for this trade, 0-1. ``None`` (the
    default) skips conviction scaling entirely, so every existing caller
    and test keeps its exact previous behaviour — opt-in by passing real
    data, exactly like ``open_interest`` above."""

    conviction_floor: float | None = None
    """``RiskCaps.min_council_confidence`` — the conviction at which a
    trade is only just allowed to exist. Scaling is measured FROM this,
    not from zero: a trade at the floor is the least-convinced trade the
    system will take, and should be the smallest, not merely proportional
    to an absolute number that never approaches 1.0 in practice."""


@dataclass(frozen=True)
class OptionsSizingDecision:
    qty: int
    """Contracts. ``floor(budget_usd / (ask * multiplier))``, never rounds
    up. 0 means even one contract exceeds the budget."""
    notes: str
    """E.g. "1 contract at $3.20 ask x100 = $320 premium, within $500
    budget" — or, on a zero result, why."""


_CONVICTION_MIN_FRACTION = 0.5
"""A floor-conviction trade gets this fraction of the full budget. Not 0 —
a trade that clears every deterministic gate is allowed to exist, and
sizing it to nothing would be a veto wearing a sizer's clothing (the
system already has named vetoes for that, and they are auditable)."""

_CONVICTION_CEILING = 0.62
"""Conviction at which a trade earns the FULL budget.

Deliberately not 1.0. The two-agent council resolves by taking the MINIMUM
of the bull and bear conviction, which compresses its output hard toward
the middle: across 151 real option decisions the entire observed range was
0.28-0.62 and the highest value ever produced was 0.62. Scaling against a
1.0 ceiling would mean no trade in the system's history ever earned more
than ~62% of its budget — the scale would be measuring a range the council
cannot reach."""


def _conviction_scaled_budget(inputs: OptionsSizingInputs) -> float:
    """Shrink the premium budget toward ``_CONVICTION_MIN_FRACTION`` as
    conviction approaches the floor.

    Returns ``budget_usd`` unchanged when either conviction input is
    absent, so this is inert for every caller that does not opt in.
    """
    conviction = inputs.conviction
    floor = inputs.conviction_floor
    if conviction is None or floor is None:
        return inputs.budget_usd
    span = _CONVICTION_CEILING - floor
    if span <= 0:
        # A floor at or above the observed ceiling leaves no range to scale
        # across. Do not silently divide by ~0 and do not invent a value:
        # fall back to full budget and let the risk rules do the refusing.
        return inputs.budget_usd
    above = (conviction - floor) / span
    above = min(1.0, max(0.0, above))
    fraction = _CONVICTION_MIN_FRACTION + (1.0 - _CONVICTION_MIN_FRACTION) * above
    return inputs.budget_usd * fraction


def options_position_size(inputs: OptionsSizingInputs) -> OptionsSizingDecision:
    """Floor-division premium sizing. Returns qty=0 (never negative, never
    rounded up) with an explanatory ``.notes`` when the budget can't afford
    even one contract — the same "convert to HOLD" shape the equity ATR
    sizer uses for its own qty<1 case."""
    if inputs.ask <= 0:
        return OptionsSizingDecision(
            qty=0,
            notes=f"ask price ${inputs.ask:.2f} is non-positive — cannot size a contract with no premium quote",
        )
    if inputs.multiplier <= 0:
        return OptionsSizingDecision(
            qty=0,
            notes=f"multiplier {inputs.multiplier} is non-positive — refusing to size",
        )
    if inputs.budget_usd <= 0:
        return OptionsSizingDecision(
            qty=0,
            notes=f"budget ${inputs.budget_usd:.2f} is non-positive — no premium available to risk",
        )

    budget_usd = _conviction_scaled_budget(inputs)
    cost_per_contract = inputs.ask * inputs.multiplier
    qty = math.floor(budget_usd / cost_per_contract)

    if qty < 1:
        return OptionsSizingDecision(
            qty=0,
            notes=(
                f"1 contract at ${inputs.ask:.2f} ask x{inputs.multiplier} = "
                f"${cost_per_contract:.2f} premium exceeds the ${inputs.budget_usd:.2f} budget"
            ),
        )

    # Liquidity trim. The dollar budget answers "what can we afford to
    # lose"; it says nothing about "can we get back out". A contract with
    # 167 open interest and one with 28,000 cost the same and so sized the
    # same, which is how CME261016P00270000 came to be a 5-lot position in
    # a contract that then gapped 26 points between prints.
    #
    # Applied AFTER the budget floor so it can only ever REDUCE a position
    # that was already affordable — it never rescues a qty<1, and it never
    # rounds a viable trade to zero (floor of 1 lot). A contract too thin
    # to hold even one lot is refused upstream by options_min_open_interest
    # and the chain-depth gate, which is where a veto belongs; sizing
    # trims, it does not veto.
    liquidity_note = ""
    if inputs.open_interest is not None and inputs.max_pct_of_open_interest > 0:
        oi_cap = math.floor(
            inputs.open_interest * inputs.max_pct_of_open_interest / 100.0
        )
        oi_cap = max(1, oi_cap)
        if oi_cap < qty:
            liquidity_note = (
                f"; trimmed from {qty} to {oi_cap} by the liquidity cap "
                f"({inputs.max_pct_of_open_interest:g}% of {inputs.open_interest} "
                f"open interest)"
            )
            qty = oi_cap

    total_premium = qty * cost_per_contract
    return OptionsSizingDecision(
        qty=qty,
        notes=(
            f"{qty} contract{'s' if qty != 1 else ''} at ${inputs.ask:.2f} ask "
            f"x{inputs.multiplier} = ${total_premium:.2f} premium, within "
            f"${inputs.budget_usd:.2f} budget{liquidity_note}"
        ),
    )
