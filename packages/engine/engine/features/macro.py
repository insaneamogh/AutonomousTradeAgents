"""Macro features — FRED series + sector relative strength.

FRED (https://fred.stlouisfed.org) is free: set ``FRED_API_KEY``. Series:

    VIXCLS     CBOE VIX close           → ``vix_level``
    DGS10      10-year Treasury yield   → ``ten_year_yield_pct``
    DTWEXBGS   Broad dollar index       → ``dxy_index``

These are the right three for a US-equity swing product: VIX is the
risk-appetite regime, DGS10 the discount-rate/duration input, DTWEXBGS
the dollar tailwind/headwind on large-cap earnings. All are FRED daily
series, so ``fred/series/observations`` is the correct endpoint —
``fred/release/observations`` returns everything in a *release* (hundreds
of unrelated series) and is for release-calendar browsing, not for
reading one series.

Values are published with up to a 1-business-day lag — fine for a
daily-bar swing product. One fetch per (series, UTC day) is cached
in-process; failures are cached briefly so a FRED outage degrades the
macro block to n/a instead of stalling the council.

``sector_relative_strength`` is computed from bars, not FRED: the symbol's
21-day return minus SPY's 21-day return, in percentage points — the same
definition the synthetic provider faked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime
from typing import Any

import httpx

from engine.features.technicals import DailyBar

logger = logging.getLogger("engine.features.macro")

_FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

_FRED_SERIES = ("VIXCLS", "DGS10", "DTWEXBGS")

# Per-request ceiling. FRED is normally <1s; anything slower is an outage,
# and the council must not wait on one.
_FRED_TIMEOUT_S = 6.0

# Wall-clock ceiling for the whole macro block, however many series it
# fetches. Bounds the council's per-symbol exposure to a hung FRED.
_FRED_BUDGET_S = 8.0

# A failed fetch is remembered this long so a FRED outage costs one timeout
# per series per five minutes, not one per series *per symbol* in a run.
_FRED_FAILURE_TTL_S = 300.0

# (series_id, utc_date) → (value_or_None, monotonic_expiry)
_fred_cache: dict[tuple[str, date], tuple[float | None, float]] = {}


def _cache_get(key: tuple[str, date]) -> tuple[float | None, bool]:
    """Cached value for ``key`` and whether the entry is still live."""
    entry = _fred_cache.get(key)
    if entry is None:
        return None, False
    value, expires_at = entry
    if expires_at <= time.monotonic():
        _fred_cache.pop(key, None)
        return None, False
    return value, True


def reset_fred_cache() -> None:
    """Drop every memoized FRED observation. For tests and manual refresh."""
    _fred_cache.clear()


async def fred_latest(series_id: str, api_key: str) -> float | None:
    """Most recent non-missing observation for a FRED series, or None.

    Never raises: a FRED outage degrades the macro block to None rather
    than failing the council. Successes are cached for the UTC day (the
    series only update once daily); failures are cached briefly so an
    outage doesn't cost a timeout on every symbol in a run.
    """
    cache_key = (series_id, datetime.now(UTC).date())
    value, live = _cache_get(cache_key)
    if live:
        return value

    params: dict[str, str | int] = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        # FRED writes "." for non-publication days (holidays, weekends for
        # the daily series). Pull a short window and take the first real one.
        "limit": 10,
    }
    try:
        async with httpx.AsyncClient(timeout=_FRED_TIMEOUT_S) as client:
            resp = await client.get(_FRED_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Never log ``exc`` directly: httpx puts the full request URL — which
        # carries ``api_key`` — into its message.
        detail = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}"
        logger.warning("macro: FRED fetch failed for %s — %s", series_id, detail)
        _fred_cache[cache_key] = (None, time.monotonic() + _FRED_FAILURE_TTL_S)
        return None

    for obs in payload.get("observations", []):
        raw = obs.get("value", ".")
        if raw in (".", "", None):
            continue
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            continue
        # Good until the end of the UTC day; the date is part of the key.
        _fred_cache[cache_key] = (parsed, time.monotonic() + 86_400.0)
        return parsed

    logger.warning("macro: no usable observation for FRED %s", series_id)
    _fred_cache[cache_key] = (None, time.monotonic() + _FRED_FAILURE_TTL_S)
    return None


async def _fred_bundle(api_key: str) -> dict[str, float | None]:
    """Fetch every macro series concurrently, under one wall-clock budget."""
    try:
        async with asyncio.timeout(_FRED_BUDGET_S):
            values = await asyncio.gather(
                *(fred_latest(s, api_key) for s in _FRED_SERIES),
                return_exceptions=True,
            )
    except TimeoutError:
        logger.warning(
            "macro: FRED bundle exceeded the %.0fs budget — macro degrades to n/a",
            _FRED_BUDGET_S,
        )
        return dict.fromkeys(_FRED_SERIES, None)

    out: dict[str, float | None] = {}
    for series_id, value in zip(_FRED_SERIES, values, strict=True):
        if isinstance(value, BaseException):
            logger.warning("macro: FRED %s raised — %s", series_id, value)
            out[series_id] = None
        else:
            out[series_id] = value
    return out


def sector_relative_strength(
    symbol_bars: list[DailyBar], spy_bars: list[DailyBar], *, window: int = 21
) -> float | None:
    """Symbol 21-day return minus SPY 21-day return, in percentage points."""
    if len(symbol_bars) <= window or len(spy_bars) <= window:
        return None
    sym_ret = symbol_bars[-1].close / symbol_bars[-1 - window].close - 1.0
    spy_ret = spy_bars[-1].close / spy_bars[-1 - window].close - 1.0
    return round((sym_ret - spy_ret) * 100.0, 2)


async def compute_macro(
    *,
    fred_api_key: str | None,
    symbol_bars: list[DailyBar],
    spy_bars: list[DailyBar],
) -> dict[str, Any]:
    """The council's ``macro`` feature block. Missing series stay None —
    prompts render 'n/a' and the Macro Analyst reasons with what exists.

    Never raises and never blocks longer than ``_FRED_BUDGET_S`` on FRED:
    macro is context, not a gate, so a FRED outage must degrade the block
    rather than fail the run.
    """
    series: dict[str, float | None] = dict.fromkeys(_FRED_SERIES, None)
    if fred_api_key:
        series = await _fred_bundle(fred_api_key)
    else:
        logger.warning("macro: FRED_API_KEY not set — VIX/10y/DXY unavailable")

    vix = series["VIXCLS"]
    ten_year = series["DGS10"]
    dxy = series["DTWEXBGS"]

    return {
        "vix_level": round(vix, 1) if vix is not None else None,
        "ten_year_yield_pct": round(ten_year, 2) if ten_year is not None else None,
        "dxy_index": round(dxy, 1) if dxy is not None else None,
        "sector_relative_strength": sector_relative_strength(symbol_bars, spy_bars),
    }
