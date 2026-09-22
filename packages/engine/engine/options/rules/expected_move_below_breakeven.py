"""Refuse a long option whose breakeven sits beyond the stock's typical move.

docs/PLAN_ENTRY_EDGE.md's missing gate. The strategies predict DIRECTION;
a long option also needs MAGNITUDE and SPEED. A right call with a small
move lost double digits three times in the recorded book (GILD155 -23.5%,
GILD150 -13.8%, AMD -10.1%), and nothing upstream could tell that trade
apart from a good one.

    required  = underlying move (thesis direction) for the option, sold
                after the HOLD at mark less half-spread, to return what was
                paid at the ask. It carries theta, both halves of the
                spread and the contract's leverage in one number
                (engine.options.breakeven).
    expected  = the stock's mean ABSOLUTE move over the same hold, from
                REALIZED vol: what a correct thesis earns on an ordinary
                day.
    refuse    when required > expected x caps.options_breakeven_move_ratio

HOLD is the time stop the position manager actually enforces for an
options entry (`HOLD_DAYS_BY_HORIZON["short"]`, 5 calendar days), not the
thesis horizon. The option lives through the hold, not the thesis.
`horizon_exceeds_contract` already handles the thesis-vs-contract mismatch.

It cannot create an edge. On a coin-flip signal it removes the trades where
even being RIGHT loses, which is the shape of the recorded losses.

Self-gates to a pass whenever it cannot assess: not an entry, the ratio
disabled, or any of spot, realized vol, IV, a two-sided quote or expiry
missing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.features.provider import HOLD_DAYS_BY_HORIZON
from engine.options.breakeven import expected_move_pct, required_move_pct
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal

OPTIONS_HOLD_HORIZON = "short"
"""The horizon `trade.py` stamps on every options entry, which sets the
position manager's time stop. Changing one without the other would make
this rule judge a hold the position never has."""


def expected_move_below_breakeven(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    ratio = caps.options_breakeven_move_ratio
    option = proposal.option
    if ratio is None or option is None or option.action != "buy_to_open":
        return None
    spot = option.underlying_price
    rv = option.underlying_realized_vol_pct
    iv = option.implied_volatility
    bid, ask = option.bid, option.ask
    if not spot or not rv or not iv or not ask or bid is None or bid < 0 or ask <= 0:
        return None

    now = context.now_utc or datetime.now(UTC)
    dte = (option.expiry - now.date()).days
    hold = HOLD_DAYS_BY_HORIZON[OPTIONS_HOLD_HORIZON]
    mid = (bid + ask) / 2.0
    half_spread_pct = (ask - mid) / mid * 100.0 if mid > 0 else 0.0

    required = required_move_pct(
        spot=spot, strike=option.strike, kind=option.contract_type, dte_days=dte,
        iv=iv, paid=ask, half_spread_pct=half_spread_pct, hold_days=hold,
    )
    expected = expected_move_pct(realized_vol_pct=rv, hold_days=hold)
    if expected is None:
        return None
    if required is not None and required <= expected * ratio:
        return None

    need = "no move within 3x of spot" if required is None else f"a {required:.2f}% move"
    return RiskDecision(
        approved=False,
        reason=(
            f"{option.occ_symbol} needs {need} in {option.underlying_symbol} over "
            f"the {hold}-day hold just to break even after theta and the spread. "
            f"This stock's typical move over that hold is {expected:.2f}% "
            f"(realized vol {rv:.1f}%), and the limit is {ratio:.2f}x that. A right "
            "call with an ordinary move would still lose. Choose a nearer-the-money "
            "or longer-dated contract, or trade the thesis in equity."
        ),
        veto_rule="expected_move_below_breakeven",
    )
