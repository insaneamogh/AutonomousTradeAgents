"""Latest quote → a ``liquidity`` feature block. Real bid/ask, honestly gated.

Bars tell you what a trade WOULD have cost yesterday. The quote tells you
what it costs to get in right now, and on a thin name that difference is
larger than the edge most of these strategies claim. The block feeds the
sizer's caller as a liquidity input and gives the analysts a real spread
instead of an assumption.

**The whole difficulty is knowing when to say None.** These keys are
entitled to the IEX feed, and an IEX-only quote outside regular trading
hours is frequently garbage — a live pull for this repo returned NVDA with
a 200.45 bid against a 0.00 ask, and AAPL 295.14 / 326.75, a 10% "spread"
that exists nowhere in the real book. Publishing those as ``spread_bps``
would put a fabricated number in front of a sizing decision.

So every quote passes four gates before it becomes a feature, and failing
any of them yields ``None`` rather than a number:

  1. Both sides quoted and positive.
  2. Both sides have size (a quote with no size behind it is not a price).
  3. ``ask > bid`` — a crossed or locked book is stale data, not an
     arbitrage we are in a position to take.
  4. The implied spread is inside ``MAX_CREDIBLE_SPREAD_BPS``. Past that
     the quote is far likelier to be an artifact of a single venue's thin
     book than a real cost of trading.

``quote_trusted`` is exported alongside the numbers so downstream code can
tell "tight spread" from "we declined to guess".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("engine.features.microstructure")

MAX_CREDIBLE_SPREAD_BPS = 200.0
"""2% wide. A US large/mid-cap in RTH quotes single-digit bps; even an
illiquid small-cap rarely exceeds ~100bps with real size. Beyond 200 we
are reading a venue artifact, not a tradable market."""

MAX_QUOTE_AGE_SECONDS = 1800.0
"""A half-hour-old quote still describes the current book well enough for a
swing entry. Older than that and the honest answer is None."""

WIDE_SPREAD_BPS = 25.0
"""Above this a proposal is flagged (not blocked) as expensive to enter.
25bps on a 2R trade is a meaningful bite out of the edge."""


@dataclass(frozen=True)
class LiquidityFeatures:
    """Bid/ask microstructure. Every numeric field is ``None`` when the
    quote failed the credibility gates — never a fabricated stand-in."""

    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread_bps: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    quote_age_seconds: float | None = None
    quote_trusted: bool = False
    """False means the numbers above are None BY DECISION, not by absence."""
    reject_reason: str | None = None
    """Named gate that rejected the quote — ``crossed_book``,
    ``no_size``, ``implausible_spread``, ``stale``, ``no_quote``. Same
    convention as ``veto_rule``: a machine-readable why."""
    wide_spread: bool | None = None
    """Trusted quote whose spread exceeds ``WIDE_SPREAD_BPS``."""

    def as_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


def compute_liquidity(
    *,
    bid: float | None,
    ask: float | None,
    bid_size: float | None,
    ask_size: float | None,
    quoted_at: datetime | None,
    now: datetime | None = None,
) -> LiquidityFeatures:
    """Apply the credibility gates to one quote. Pure — no network, no SDK.

    Split from the provider precisely so the gates are testable against the
    exact garbage the live feed produces after hours.
    """
    at = now or datetime.now(UTC)

    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return LiquidityFeatures(reject_reason="no_quote")
    if not bid_size or not ask_size or bid_size <= 0 or ask_size <= 0:
        return LiquidityFeatures(reject_reason="no_size")
    if ask <= bid:
        return LiquidityFeatures(reject_reason="crossed_book")

    age = None
    if quoted_at is not None:
        stamped = quoted_at if quoted_at.tzinfo else quoted_at.replace(tzinfo=UTC)
        age = round((at - stamped).total_seconds(), 1)
        if age > MAX_QUOTE_AGE_SECONDS:
            return LiquidityFeatures(quote_age_seconds=age, reject_reason="stale")

    mid = (bid + ask) / 2.0
    spread_bps = ((ask - bid) / mid) * 10_000.0
    if spread_bps > MAX_CREDIBLE_SPREAD_BPS:
        return LiquidityFeatures(
            quote_age_seconds=age, reject_reason="implausible_spread"
        )

    return LiquidityFeatures(
        bid=round(bid, 4),
        ask=round(ask, 4),
        mid=round(mid, 4),
        spread_bps=round(spread_bps, 2),
        bid_size=bid_size,
        ask_size=ask_size,
        quote_age_seconds=age,
        quote_trusted=True,
        wide_spread=spread_bps > WIDE_SPREAD_BPS,
    )


@runtime_checkable
class QuoteProvider(Protocol):
    name: str

    async def liquidity(self, symbol: str) -> LiquidityFeatures: ...


class AlpacaSnapshotProvider:
    """``/v2/stocks/{symbol}/snapshot`` (IEX feed) → LiquidityFeatures."""

    name = "alpaca-snapshot"

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._client = StockHistoricalDataClient(self._api_key, self._secret_key)
        return self._client

    async def liquidity(self, symbol: str) -> LiquidityFeatures:
        sym = symbol.upper()
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockSnapshotRequest

        req = StockSnapshotRequest(symbol_or_symbols=[sym], feed=DataFeed.IEX)
        try:
            snaps = await asyncio.to_thread(self._get_client().get_stock_snapshot, req)
        except Exception:
            logger.exception("microstructure: snapshot fetch failed for %s", sym)
            return LiquidityFeatures(reject_reason="no_quote")

        snap = snaps.get(sym) if hasattr(snaps, "get") else None
        quote = getattr(snap, "latest_quote", None) if snap is not None else None
        if quote is None:
            return LiquidityFeatures(reject_reason="no_quote")

        return compute_liquidity(
            bid=_f(getattr(quote, "bid_price", None)),
            ask=_f(getattr(quote, "ask_price", None)),
            bid_size=_f(getattr(quote, "bid_size", None)),
            ask_size=_f(getattr(quote, "ask_size", None)),
            quoted_at=getattr(quote, "timestamp", None),
        )


def _f(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def snapshot_provider_from_env() -> AlpacaSnapshotProvider | None:
    """Real snapshot provider when Alpaca data keys are set; otherwise None."""
    import os

    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret:
        return None
    return AlpacaSnapshotProvider(api_key, secret)
