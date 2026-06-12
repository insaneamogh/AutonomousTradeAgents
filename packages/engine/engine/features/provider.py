"""RealFeatureProvider — assembles the council's feature dict from live data.

Same shape as ``trading_agents.features.synthetic_features`` so the agents
and prompts don't change:

    {symbol, horizon, universe, last_price, portfolio_equity,
     technicals{...}, macro{...}, fundamentals{...}?}

Sources:
  - technicals + last_price : Alpaca IEX daily bars → ``compute_technicals``
  - macro                   : FRED (VIX / 10y / dollar) + SPY relative strength
  - portfolio_equity        : injected ``equity_resolver`` (latest reconciler
                              snapshot in production); falls back to the
                              100k fixture with a loud log
  - fundamentals            : OPTIONAL ``FundamentalsProvider``. When absent
                              the key is OMITTED ENTIRELY — never synthetic
                              numbers — and the Router drops the Fundamental
                              Analyst for the run (it has nothing real to
                              read).

``feature_provider_from_env()`` is the factory the cron/API use: real
provider when Alpaca data keys exist, else None (caller falls back to
synthetic for dev, or hard-fails under AGENTS_REQUIRE_REAL_DATA).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from engine.features.bars import AlpacaDailyBarsProvider, BarsProvider
from engine.features.macro import compute_macro
from engine.features.technicals import InsufficientBarsError, compute_technicals

logger = logging.getLogger("engine.features.provider")

DEFAULT_EQUITY_FALLBACK = 100_000.0


@runtime_checkable
class FundamentalsProvider(Protocol):
    """Seam for a real fundamentals source (FMP / Polygon bundled / …).

    Must return the ``fundamentals`` block (quality_score,
    earnings_power_score, …) computed from REAL filings data, or None when
    the symbol isn't covered. No implementation ships until a data
    subscription is wired — the Router excludes the Fundamental Analyst
    in the meantime.
    """

    name: str

    async def fetch(self, symbol: str) -> dict[str, Any] | None: ...


@dataclass
class RealFeatureProvider:
    bars: BarsProvider
    fred_api_key: str | None = None
    fundamentals: FundamentalsProvider | None = None
    equity_resolver: Callable[[], Awaitable[float | None]] | None = None
    universe: str = "US"

    async def __call__(self, symbol: str, horizon: str = "short") -> dict[str, Any]:
        sym = symbol.upper()
        bars = await self.bars.daily_bars(sym)
        if not bars:
            raise InsufficientBarsError(f"no daily bars available for {sym}")
        spy_bars = await self.bars.daily_bars("SPY", lookback_days=60)

        technicals = compute_technicals(bars)
        macro = await compute_macro(
            fred_api_key=self.fred_api_key, symbol_bars=bars, spy_bars=spy_bars
        )

        equity: float | None = None
        if self.equity_resolver is not None:
            try:
                equity = await self.equity_resolver()
            except Exception:  # noqa: BLE001
                logger.exception("features: equity resolver failed — using fallback")
        if equity is None or equity <= 0:
            logger.warning(
                "features: no real portfolio equity available — sizing will use "
                "the %.0f fixture. Wire an equity_resolver before real trading.",
                DEFAULT_EQUITY_FALLBACK,
            )
            equity = DEFAULT_EQUITY_FALLBACK

        features: dict[str, Any] = {
            "symbol": sym,
            "horizon": horizon,
            "universe": self.universe,
            "last_price": bars[-1].close,
            "portfolio_equity": equity,
            "technicals": technicals,
            "macro": macro,
            "feature_source": "alpaca",
        }

        if self.fundamentals is not None:
            try:
                fund = await self.fundamentals.fetch(sym)
            except Exception:  # noqa: BLE001
                logger.exception("features: fundamentals fetch failed for %s", sym)
                fund = None
            if fund:
                features["fundamentals"] = fund
        # NOTE: no fundamentals key at all when there's no real source.
        # The Router post-filter drops the Fundamental Analyst for this run.

        return features


def feature_provider_from_env(
    *,
    equity_resolver: Callable[[], Awaitable[float | None]] | None = None,
    fundamentals: FundamentalsProvider | None = None,
) -> RealFeatureProvider | None:
    """Real provider when Alpaca data keys are set; otherwise None."""
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret:
        return None
    fred_key = os.environ.get("FRED_API_KEY", "").strip() or None
    return RealFeatureProvider(
        bars=AlpacaDailyBarsProvider(api_key, secret),
        fred_api_key=fred_key,
        fundamentals=fundamentals,
        equity_resolver=equity_resolver,
    )
