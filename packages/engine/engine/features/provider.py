"""RealFeatureProvider — assembles the council's feature dict from live data.

Same shape as ``trading_agents.features.synthetic_features`` so the agents
and prompts don't change:

    {symbol, horizon, universe, last_price, portfolio_equity,
     technicals{...}, macro{...}, fundamentals{...}?}

Sources:
  - technicals + last_price : Alpaca IEX daily bars → ``compute_technicals``
  - quant                   : the same bars + SPY → ``compute_quant``
                              (realized/Parkinson/Garman-Klass vol, Sharpe,
                              Sortino, max drawdown, beta + correlation to
                              SPY, skew/kurtosis, standardized price z-score)
  - news                    : OPTIONAL Alpaca /v1beta1/news → deterministic
                              coverage stats + sanitized headlines. This is
                              the Fundamental Analyst's only real input
                              until a filings vendor is wired.
  - liquidity               : OPTIONAL Alpaca snapshot → bid/ask spread,
                              gated hard (see ``microstructure``) so an
                              after-hours IEX quote never becomes a number
  - events                  : OPTIONAL Alpaca corporate actions → ex-div /
                              split inside the holding horizon
  - asset                   : OPTIONAL broker asset record → shortable +
                              easy_to_borrow. REQUIRED for any short: the
                              ``shortable_check`` risk rule vetoes when it
                              is missing.
  - options_context         : OPTIONAL (Phase A). ``iv_rank``/``atm_iv``/
                              ``term_structure_slope``/``days_to_earnings``
                              (all ``None`` until a real options-analytics
                              source is wired — see
                              ``MinimalOptionsContextProvider``), plus
                              ALWAYS-populated ``data_delay_minutes``/
                              ``feed_type`` for the UI's "delayed data"
                              badge.
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

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from engine.features.bars import AlpacaDailyBarsProvider, BarsProvider
from engine.features.corporate_actions import (
    CorporateActionsProvider,
    compute_corporate_actions,
    corporate_actions_provider_from_env,
)
from engine.features.macro import compute_macro
from engine.features.microstructure import (
    QuoteProvider,
    snapshot_provider_from_env,
)
from engine.features.news import FETCH_LIMIT as NEWS_FETCH_LIMIT
from engine.features.news import (
    NewsProvider,
    compute_news,
    news_provider_from_env,
)
from engine.features.quant import compute_quant
from engine.features.technicals import InsufficientBarsError, compute_technicals

logger = logging.getLogger("engine.features.provider")

DEFAULT_EQUITY_FALLBACK = 100_000.0

# Benchmark history pulled for the quant block's beta/correlation.
SPY_LOOKBACK_DAYS = 320

# Time stop per horizon — mirrors the Drafter's map. The corporate-action
# block asks "does anything land while we would still be holding", so the
# horizon has to be the same number the exit plan promises.
HOLD_DAYS_BY_HORIZON: dict[str, int] = {
    "intraday": 1,
    "short": 5,
    "mid": 10,
    "long": 20,
}


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


@runtime_checkable
class AssetInfoProvider(Protocol):
    """Seam for the broker's asset record (shortable / easy-to-borrow).

    Separated from the bars provider because it is a TRADING-API question,
    not a market-data one, and because the risk engine's short rules treat
    a missing answer as a veto — that has to be an explicit dependency, not
    an incidental one.
    """

    name: str

    async def fetch(self, symbol: str) -> dict[str, Any] | None: ...


@runtime_checkable
class OptionsContextProvider(Protocol):
    """Seam for a real options-market data source (IV rank, term structure,
    days-to-earnings vs. the underlying's own history).

    Same optional/failure-tolerant contract as ``FundamentalsProvider``/
    ``AssetInfoProvider`` above: returns the ``options_context`` block or
    None. No real market-data-backed implementation ships yet — see
    ``MinimalOptionsContextProvider`` below — because no real IV-rank/term-
    structure source is wired anywhere in this repo as of Phase A. Building
    one is a follow-up; this seam exists so it can slot in without another
    ``_optional_blocks`` change.
    """

    name: str

    async def fetch(self, symbol: str) -> dict[str, Any] | None: ...


# Always-populated regardless of what the rest of the block knows — these
# describe the DATA FEED itself (docs/OPTIONS_PLAN.md §0: Alpaca's free
# Basic tier is an indicative, 15-minute-delayed feed, not full OPRA), not
# a per-symbol fact that could be legitimately unknown. The UI's "delayed
# data" badge reads these and must never find them missing.
_OPTIONS_DATA_DELAY_MINUTES = 15
_OPTIONS_FEED_TYPE = "indicative_delayed"


class MinimalOptionsContextProvider:
    """Phase-A placeholder: the always-populated feed-quality fields, and
    ``None`` for everything that needs a real IV-rank/term-structure data
    source. Deliberately not synthetic — a fabricated IV rank would be
    worse than an absent one, since ``select_contract``/the options risk
    rules would have no way to tell a real number from a guess.

    FOLLOW-UP: replace with a real implementation once an options-analytics
    source is chosen (docs/OPTIONS_PLAN.md §6) — compute ``iv_rank``/
    ``atm_iv``/``term_structure_slope`` from the underlying's own IV
    history, and ``days_to_earnings`` from the same corporate-actions
    source ``compute_corporate_actions`` already uses for ``events``.
    """

    name = "options-context-minimal"

    async def fetch(self, symbol: str) -> dict[str, Any]:
        return {
            "iv_rank": None,
            "atm_iv": None,
            "term_structure_slope": None,
            "days_to_earnings": None,
            "data_delay_minutes": _OPTIONS_DATA_DELAY_MINUTES,
            "feed_type": _OPTIONS_FEED_TYPE,
        }


class AlpacaAssetInfoProvider:
    """``broker.alpaca.lookup_asset`` → the ``asset`` feature block.

    Cached per symbol for the process lifetime: listing status and borrow
    eligibility change on the order of days, and the daily cron is a fresh
    process per run.
    """

    name = "alpaca-asset"

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._cache: dict[str, dict[str, Any] | None] = {}

    async def fetch(self, symbol: str) -> dict[str, Any] | None:
        sym = symbol.upper()
        if sym in self._cache:
            return self._cache[sym]

        from broker.alpaca import lookup_asset

        info = await lookup_asset(sym, api_key=self._api_key, secret_key=self._secret_key)
        block: dict[str, Any] | None = None
        if info is not None:
            block = {
                "tradable": info.tradable,
                "fractionable": info.fractionable,
                "shortable": info.shortable,
                "easy_to_borrow": info.easy_to_borrow,
                "name": info.name,
            }
        self._cache[sym] = block
        return block


@dataclass
class RealFeatureProvider:
    bars: BarsProvider
    fred_api_key: str | None = None
    fundamentals: FundamentalsProvider | None = None
    equity_resolver: Callable[[], Awaitable[float | None]] | None = None
    universe: str = "US"
    news: NewsProvider | None = None
    quotes: QuoteProvider | None = None
    corporate_actions: CorporateActionsProvider | None = None
    asset_info: AssetInfoProvider | None = None
    options_context: OptionsContextProvider | None = None

    async def __call__(self, symbol: str, horizon: str = "short") -> dict[str, Any]:
        sym = symbol.upper()
        bars = await self.bars.daily_bars(sym)
        if not bars:
            raise InsufficientBarsError(f"no daily bars available for {sym}")
        # 320 days (~220 trading bars), not 60: the quant block regresses
        # this symbol against SPY over a 63-day window and z-scores ATR
        # against a year of its own history. A 60-day SPY pull would leave
        # beta/correlation permanently None. Provider-cached, so the longer
        # window costs one request per process, not one per symbol.
        spy_bars = await self.bars.daily_bars("SPY", lookback_days=SPY_LOOKBACK_DAYS)

        technicals = compute_technicals(bars)
        quant = compute_quant(bars, benchmark_bars=spy_bars)
        macro = await compute_macro(
            fred_api_key=self.fred_api_key, symbol_bars=bars, spy_bars=spy_bars
        )

        equity: float | None = None
        if self.equity_resolver is not None:
            try:
                equity = await self.equity_resolver()
            except Exception:
                logger.exception("features: equity resolver failed — using fallback")
        if equity is None or equity <= 0:
            logger.warning(
                "features: no real portfolio equity available — sizing will use "
                "the %.0f fixture. Wire an equity_resolver before real trading.",
                DEFAULT_EQUITY_FALLBACK,
            )
            equity = DEFAULT_EQUITY_FALLBACK

        extras = await self._optional_blocks(sym, horizon, bars[-1].close)

        features: dict[str, Any] = {
            "symbol": sym,
            "horizon": horizon,
            "universe": self.universe,
            "last_price": bars[-1].close,
            "portfolio_equity": equity,
            "technicals": technicals,
            "quant": quant.as_dict(),
            "macro": macro,
            "feature_source": "alpaca",
            **extras,
        }

        if self.fundamentals is not None:
            try:
                fund = await self.fundamentals.fetch(sym)
            except Exception:
                logger.exception("features: fundamentals fetch failed for %s", sym)
                fund = None
            if fund:
                features["fundamentals"] = fund
        # NOTE: no fundamentals key at all when there's no real source.
        # The Router post-filter drops the Fundamental Analyst for this run.

        return features


    async def _optional_blocks(
        self, symbol: str, horizon: str, last_price: float
    ) -> dict[str, Any]:
        """News / liquidity / corporate-action / asset blocks, gathered concurrently.

        Every one of these is OPTIONAL and independently failure-tolerant.
        A provider that raises contributes no key at all rather than a
        half-filled one — the same rule the fundamentals block follows, and
        for the same reason: a downstream reader must be able to treat
        "key absent" as "we do not know", never as "we know it is zero".

        The four are gathered rather than awaited in sequence: they are
        four independent HTTP round-trips against the same host, and doing
        them serially would add most of a second to every symbol.
        """
        jobs: list[tuple[str, Any]] = []
        if self.news is not None:
            jobs.append(("news", self.news.fetch(symbol)))
        if self.quotes is not None:
            jobs.append(("liquidity", self.quotes.liquidity(symbol)))
        if self.corporate_actions is not None:
            jobs.append(("events", self.corporate_actions.fetch(symbol)))
        if self.asset_info is not None:
            jobs.append(("asset", self.asset_info.fetch(symbol)))
        if self.options_context is not None:
            jobs.append(("options_context", self.options_context.fetch(symbol)))
        if not jobs:
            return {}

        results = await asyncio.gather(
            *(job for _, job in jobs), return_exceptions=True
        )
        out: dict[str, Any] = {}
        for (key, _), result in zip(jobs, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "features: %s block unavailable for %s (%s) — omitting the key",
                    key, symbol, result,
                )
                continue
            if key == "news":
                out["news"] = compute_news(
                    result, truncated=len(result) >= NEWS_FETCH_LIMIT
                ).as_dict()
            elif key == "liquidity":
                out["liquidity"] = result.as_dict()
            elif key == "events":
                out["events"] = compute_corporate_actions(
                    result,
                    horizon_days=HOLD_DAYS_BY_HORIZON.get(horizon, 5),
                    last_price=last_price,
                ).as_dict()
            elif key == "asset" and result is not None:
                out["asset"] = result
            elif key == "options_context" and result is not None:
                out["options_context"] = result
        return out


def feature_provider_from_env(
    *,
    equity_resolver: Callable[[], Awaitable[float | None]] | None = None,
    fundamentals: FundamentalsProvider | None = None,
) -> RealFeatureProvider | None:
    """Real provider when Alpaca data keys are set; otherwise None.

    The same keys entitle every optional block, so they all come along —
    news, quote-derived liquidity, corporate actions, and the asset/borrow
    record. Nothing here costs extra; the reason they were not wired before
    is that nobody had written the deterministic reducers.
    """
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
        news=news_provider_from_env(),
        quotes=snapshot_provider_from_env(),
        corporate_actions=corporate_actions_provider_from_env(),
        asset_info=AlpacaAssetInfoProvider(api_key, secret),
        # Unconditional, like every sibling slot above — the block itself
        # is cheap (no real network call yet, see MinimalOptionsContextProvider)
        # and its always-populated feed-quality fields are meant to be
        # available whenever features are, not gated behind ALLOW_OPTIONS.
        # Whether a run actually ACTS on days_to_earnings is the Drafter's
        # ALLOW_OPTIONS + instrument_preference gate, not this provider's.
        options_context=MinimalOptionsContextProvider(),
    )
