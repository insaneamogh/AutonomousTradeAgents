"""Env-driven provider selection.

``ALPACA_API_KEY`` + ``ALPACA_SECRET_KEY`` present → real daily bars.
Otherwise the synthetic walk (anchored per call site) keeps every
feature working in MOCK mode.
"""

from __future__ import annotations

import os
from datetime import date

from engine.prices.base import PriceProvider
from engine.prices.synthetic import SyntheticPriceProvider


def get_price_provider(
    *,
    anchor_price: float = 100.0,
    anchor_day: date | None = None,
) -> PriceProvider:
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if api_key and secret:
        from engine.prices.alpaca import AlpacaPriceProvider

        return AlpacaPriceProvider(api_key, secret)
    return SyntheticPriceProvider(anchor_price=anchor_price, anchor_day=anchor_day)


def get_option_price_provider(
    *,
    anchor_price: float = 1.0,
    anchor_day: date | None = None,
) -> PriceProvider:
    """The OPTION-contract twin of ``get_price_provider``.

    Separate function rather than a flag on the one above because the two
    resolve to genuinely different Alpaca endpoints (stock bars vs option
    bars) keyed by genuinely different symbols (underlying vs OCC). A
    caller that picks the wrong one gets an empty series, not an error —
    so the choice is made explicitly at the call site.

    The synthetic fallback anchors at 1.0 rather than 100.0: an option
    premium is single-digit dollars, and a 100.0 anchor would produce
    ghost P&L two orders of magnitude too large in MOCK mode.
    """
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if api_key and secret:
        from engine.prices.option_alpaca import AlpacaOptionPriceProvider

        return AlpacaOptionPriceProvider(api_key, secret)
    return SyntheticPriceProvider(anchor_price=anchor_price, anchor_day=anchor_day)
