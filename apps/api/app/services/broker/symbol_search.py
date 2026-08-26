"""Ticker typeahead over the broker's tradable universe.

The Run box used to take free text, so "apple" was upper-cased to
"APPLE", passed the shape regex, and only failed once the broker
rejected the order — after a full council pass had been spent on it.
Validating harder fixes the symptom; letting the user *pick* from real
symbols removes the class of mistake.

The universe is ~13.4k active US equities and ETFs and takes ~2s to
fetch, so it is loaded once and cached in-process. Listings change on
the order of days, hence a long TTL and a lazy refresh — never a fetch
per keystroke.

In-process cache, single-instance: same tradeoff the rate limiter and
run registry already make (``UVICORN_WORKERS`` is pinned to 1). Each
replica would just hold its own copy, which is harmless here because
the data is public and read-only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger("api.services.broker.symbol_search")

_TTL_SECONDS = 12 * 60 * 60  # listings change daily at most

_cache: list[SymbolHit] = []
_cached_at: float = 0.0
_lock = asyncio.Lock()


@dataclass(frozen=True)
class SymbolHit:
    symbol: str
    name: str
    fractionable: bool


def _keys() -> tuple[str, str] | None:
    k = os.environ.get("ALPACA_API_KEY", "").strip()
    s = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    return (k, s) if k and s else None


async def _universe() -> list[SymbolHit]:
    """Cached tradable universe. Empty list when no data keys are set."""
    global _cache, _cached_at

    if _cache and (time.monotonic() - _cached_at) < _TTL_SECONDS:
        return _cache

    creds = _keys()
    if creds is None:
        return []

    async with _lock:
        # Re-check: another request may have refreshed while we waited.
        if _cache and (time.monotonic() - _cached_at) < _TTL_SECONDS:
            return _cache
        try:
            from broker.alpaca import list_tradable_assets

            assets = await list_tradable_assets(api_key=creds[0], secret_key=creds[1])
        except Exception:
            logger.exception("symbol universe refresh failed — serving stale cache")
            return _cache

        _cache = [
            SymbolHit(symbol=a.symbol, name=a.name, fractionable=a.fractionable)
            for a in assets
        ]
        _cached_at = time.monotonic()
        logger.info("symbol universe cached: %d tradable symbols", len(_cache))

    return _cache


def _score(hit: SymbolHit, q: str) -> int | None:
    """Rank a match, lower is better. None means no match.

    Ordering intent: someone typing "AAPL" wants Apple first, and someone
    typing "apple" also wants Apple — not the leveraged Apple ETF that
    happens to start with the same letters. Exact ticker beats prefix
    ticker beats name-start beats name-substring.
    """
    sym = hit.symbol.upper()
    name = hit.name.upper()

    if sym == q:
        return 0
    if sym.startswith(q):
        return 1
    if name.startswith(q):
        return 2
    # Word-boundary hit in the name ("APPLE" in "T-REX 2X LONG APPLE …")
    if any(w.startswith(q) for w in name.replace("-", " ").split()):
        return 3
    if q in name:
        return 4
    return None


async def search_symbols(query: str, *, limit: int = 10) -> list[SymbolHit]:
    """Ranked ticker/name matches for a typeahead. Empty query → []."""
    q = query.strip().upper()
    if not q:
        return []

    universe = await _universe()
    scored: list[tuple[int, int, SymbolHit]] = []
    for hit in universe:
        rank = _score(hit, q)
        if rank is not None:
            # Tie-break on symbol length so AAPL sorts above AAPLW.
            scored.append((rank, len(hit.symbol), hit))

    scored.sort(key=lambda t: (t[0], t[1], t[2].symbol))
    return [hit for _, _, hit in scored[:limit]]


async def warm_symbol_cache() -> int:
    """Populate the cache at startup so the first search isn't slow."""
    return len(await _universe())


async def assert_tradable(symbol: str) -> None:
    """Raise 422 unless ``symbol`` is a tradable US equity/ETF at the broker.

    Shared by every entry point that accepts a ticker. ``SYMBOL_RE`` only
    proves the SHAPE is right — "APPLE" and "BANANA" pass it — so without
    this a bad ticker reaches a council pass (six LLM calls) or sits in a
    watchlist failing every scheduled sweep.

    No-ops when data keys are absent (dev/MOCK), and allows the symbol
    through on a lookup outage: a broker hiccup must not block trading.
    """
    from fastapi import HTTPException

    creds = _keys()
    if creds is None:
        return

    from broker.alpaca import lookup_asset

    try:
        asset = await lookup_asset(symbol, api_key=creds[0], secret_key=creds[1])
    except Exception:  # noqa: BLE001
        logger.warning("symbol lookup failed for %s — allowing it through", symbol)
        return

    if asset is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{symbol} is not a tradable US equity or ETF on this broker. "
                "Use the ticker rather than the company name (AAPL, not Apple)."
            ),
        )
    if not asset.tradable:
        raise HTTPException(
            status_code=422,
            detail=f"{symbol} ({asset.name}) is not currently tradable on this broker.",
        )
