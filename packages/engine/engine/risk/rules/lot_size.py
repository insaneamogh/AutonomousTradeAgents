"""lot_size_block — India F&O orders must be whole-lot multiples.

NSE/BSE derivatives trade in contract lots (e.g. NIFTY = 75 units/lot as
of the 2024 revision). The exchange rejects off-lot quantities anyway, but
catching it here gives the audit log a named rule instead of a raw broker
error, and keeps a bad sizer from burning the order round-trip.

Underlying resolution is longest-prefix match of the tradingsymbol against
``caps.lot_sizes`` (so BANKNIFTY wins over NIFTY). Unknown underlyings get
an INFORMATIONAL flag, not a veto — single-stock F&O lot sizes change per
contract and the registry only carries the index majors by default.

veto_rule: lot_size_block
informational flag: lot_size_unverified:<symbol>
"""

from __future__ import annotations

from engine.risk.markets import is_derivative, tradingsymbol_of
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def _lot_size_for(tradingsymbol: str, caps: RiskCaps) -> int | None:
    """Longest-prefix match against the registry; None when unknown."""
    best: tuple[int, int] | None = None  # (prefix_len, lot)
    for underlying, lot in caps.lot_sizes:
        if tradingsymbol.startswith(underlying):
            if best is None or len(underlying) > best[0]:
                best = (len(underlying), lot)
    return best[1] if best else None


def lot_size_block(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if not is_derivative(proposal.symbol):
        return None

    tradingsymbol = tradingsymbol_of(proposal.symbol)
    lot = _lot_size_for(tradingsymbol, caps)
    if lot is None:
        return RiskDecision(
            approved=True,
            reason=f"Lot size for {proposal.symbol} not in registry — unverified.",
            informational_flags=(f"lot_size_unverified:{proposal.symbol}",),
        )

    if proposal.qty <= 0 or proposal.qty % lot != 0:
        return RiskDecision(
            approved=False,
            reason=(
                f"{proposal.symbol} trades in lots of {lot}; qty {proposal.qty} "
                f"is not a whole multiple. Nearest valid sizes: "
                f"{max(lot, (proposal.qty // lot) * lot)} or {((proposal.qty // lot) + 1) * lot}."
            ),
            veto_rule="lot_size_block",
        )
    return None
