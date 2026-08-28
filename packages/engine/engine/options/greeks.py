"""Greeks — Phase A stub, deliberately not a pricing model.

Phase A's only greek-aware risk rule is ``iv_unavailable``, and it reads
``OptionLegDetails.implied_volatility`` directly — answering "can we price
this contract" needs no model, just a null-check on whatever the feed
already reported. This module exists so a caller working from a raw
chain/snapshot fetch (before an ``OptionLegDetails`` even exists — e.g.
the separately-built ``engine.options.selection`` chain scan) has one
typed shape for "whatever greeks we got back," instead of inventing an ad
hoc dict per call site, and so a future Phase B/C portfolio-greek-cap rule
(``portfolio_delta_cap``/``portfolio_theta_cap`` — deliberately deferred,
see docs/OPTIONS_PLAN.md §2.5) has somewhere to land.

Deliberately NOT a Black-Scholes implementation: Alpaca's own snapshot
already reports greeks on liquid near-the-money strikes (missing on deep
ITM and some 0DTE — docs/OPTIONS_PLAN.md §0), and Phase A never needs to
compute one from scratch to answer the one question it actually asks.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Greeks:
    """Whatever greeks a caller already has from a chain/snapshot fetch.

    Every field is optional because the feed omitting them is the normal
    case this type exists to represent, not an error state.
    """

    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    implied_volatility: float | None = None

    @property
    def iv_unavailable(self) -> bool:
        """True when there is no IV to price the contract with — mirrors
        ``OptionLegDetails.implied_volatility is None`` for callers working
        from a ``Greeks`` snapshot rather than the leg details directly."""
        return self.implied_volatility is None
