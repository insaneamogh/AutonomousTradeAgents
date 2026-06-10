"""US equity transaction-cost model.

References (Apr 2026 rates — verify before going to live capital):
    SEC Section 31 fee: $0.0000278 per dollar SOLD (sells only).
    FINRA TAF (Trading Activity Fee): $0.000166 per share SOLD, max $8.30/order.
    Alpaca: $0 commission on US equities.

Slippage in v1: fixed-bps function. Phase 1 adds spread- and volume-
participation models.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from broker.types import Side

SlippageFn = Callable[[Side, float], float]

SEC_FEE_PER_DOLLAR_SOLD = 0.0000278
FINRA_TAF_PER_SHARE_SOLD = 0.000166
FINRA_TAF_MAX_PER_ORDER = 8.30


@dataclass(frozen=True)
class SecFinraTafCosts:
    """SEC + FINRA TAF fee schedule. Sells only — buys are free under both."""

    sec_per_dollar_sold: float = SEC_FEE_PER_DOLLAR_SOLD
    finra_per_share_sold: float = FINRA_TAF_PER_SHARE_SOLD
    finra_max_per_order: float = FINRA_TAF_MAX_PER_ORDER

    def fees_for(self, side: Side, qty: int, fill_price: float) -> tuple[float, float]:
        """Returns ``(sec_fee, finra_taf)`` for the given fill. Buys → (0, 0)."""
        if side is not Side.SELL or qty <= 0:
            return (0.0, 0.0)
        notional = qty * fill_price
        sec = round(notional * self.sec_per_dollar_sold, 4)
        finra = min(qty * self.finra_per_share_sold, self.finra_max_per_order)
        return (sec, round(finra, 4))


def fixed_bps_slippage(bps: float) -> SlippageFn:
    """Returns a slippage function: BUY gets price * (1 + bps/10000), SELL the inverse."""
    factor = bps / 10_000.0

    def _apply(side: Side, reference_price: float) -> float:
        if side is Side.BUY:
            return reference_price * (1.0 + factor)
        return reference_price * (1.0 - factor)

    return _apply
