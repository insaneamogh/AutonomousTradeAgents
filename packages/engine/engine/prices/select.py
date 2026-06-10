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
