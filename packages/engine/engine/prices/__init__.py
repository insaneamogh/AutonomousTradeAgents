"""Price providers — daily closes for ghost-P&L evaluation.

Deterministic data layer; no LLM anywhere near it. ``get_price_provider``
selects Alpaca market data when API keys are present, otherwise the
seeded synthetic walk (MOCK-mode parity with ``features.synthetic``).
"""

from engine.prices.base import DailyClose, PriceProvider
from engine.prices.select import get_price_provider
from engine.prices.synthetic import SyntheticPriceProvider

__all__ = [
    "DailyClose",
    "PriceProvider",
    "SyntheticPriceProvider",
    "get_price_provider",
]
