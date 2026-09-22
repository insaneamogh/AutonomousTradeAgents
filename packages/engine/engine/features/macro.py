"""Macro features — FRED series + sector relative strength.

FRED (https://fred.stlouisfed.org) is free: set ``FRED_API_KEY``. Series:

    VIXCLS     CBOE VIX close           → ``vix_level``
    DGS10      10-year Treasury yield   → ``ten_year_yield_pct``
    DTWEXBGS   Broad dollar index       → ``dxy_index`` / ``dxy_zscore_1y``

Plus two derived fields, from the same one request per series:
``ten_year_change_63d_bp`` (is the 10y rising or falling, and how fast) and
``dxy_zscore_1y`` (is the dollar strong for its OWN recent history). The
macro prompt's heuristics ask exactly those two questions, and a single
level per series could not answer either.

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

# Observations requested per series: about 14 months of a daily series,
# weekends and holidays included, which comfortably covers the 252-trading-
# day window `dxy_zscore_1y` needs. Still ONE request per series per UTC
# day, the same request count as when this fetched only the latest value.
_FRED_HISTORY_OBS = 300

# Trading-day windows for the derived fields.
_RATE_CHANGE_WINDOW = 63     # ~3 months
_ZSCORE_WINDOW = 252         # ~1 year
_ZSCORE_MIN_OBS = 60

# (series_id, utc_date) → (valid observations newest-first or None, monotonic_expiry)
_fred_cache: dict[tuple[str, date], tuple[tuple[float, ...] | None, float]] = {}


def _cache_get(key: tuple[str, date]) -> tuple[tuple[float, ...] | None, bool]:
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
    than failing the council. Served from ``fred_history``'s daily cache,
    so reading the latest value and the history costs one request.
    """
    history = await fred_history(series_id, api_key)
    return history[0] if history else None


async def fred_history(series_id: str, api_key: str) -> tuple[float, ...] | None:
    """Valid observations for a FRED series, NEWEST FIRST, or None.

    Never raises. Successes are cached for the UTC day (the series only
    update once daily); failures are cached briefly so an outage doesn't
    cost a timeout on every symbol in a run.
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
        # the daily series). Those are skipped below, never read as zero.
        "limit": _FRED_HISTORY_OBS,
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

    values: list[float] = []
    for obs in payload.get("observations", []):
        raw = obs.get("value", ".")
        if raw in (".", "", None):
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue

    if not values:
        logger.warning("macro: no usable observation for FRED %s", series_id)
        _fred_cache[cache_key] = (None, time.monotonic() + _FRED_FAILURE_TTL_S)
        return None
    history = tuple(values)
    # Good until the end of the UTC day; the date is part of the key.
    _fred_cache[cache_key] = (history, time.monotonic() + 86_400.0)
    return history


def change_bp(history: tuple[float, ...] | None, window: int) -> float | None:
    """Latest minus the value ``window`` observations earlier, in basis
    points (series in percent, x100). None without enough history."""
    if not history or len(history) <= window:
        return None
    return round((history[0] - history[window]) * 100.0, 1)


def zscore_latest(
    history: tuple[float, ...] | None, window: int, *, min_obs: int
) -> float | None:
    """Z-score of the latest value against the trailing ``window``
    observations (latest included). None when there are fewer than
    ``min_obs`` points or no variance.

    Exists because an index LEVEL is not comparable to a fixed threshold
    unless you know the index's scale. The macro prompt said "DXY > 105",
    the ICE dollar index's scale, while this block serves FRED DTWEXBGS,
    the Fed's broad index (Jan 2006 = 100), which has sat far above 105
    for years. So "strong dollar" read as permanently on. A z-score
    against the series' own year is scale-free."""
    if not history or len(history) < min_obs:
        return None
    ref = history[:window]
    n = len(ref)
    mean = sum(ref) / n
    var = sum((x - mean) ** 2 for x in ref) / (n - 1)
    if var <= 0:
        return None
    return round((history[0] - mean) / var**0.5, 2)


async def _fred_bundle(api_key: str) -> dict[str, tuple[float, ...] | None]:
    """Fetch every macro series concurrently, under one wall-clock budget."""
    try:
        async with asyncio.timeout(_FRED_BUDGET_S):
            values = await asyncio.gather(
                *(fred_history(s, api_key) for s in _FRED_SERIES),
                return_exceptions=True,
            )
    except TimeoutError:
        logger.warning(
            "macro: FRED bundle exceeded the %.0fs budget — macro degrades to n/a",
            _FRED_BUDGET_S,
        )
        return dict.fromkeys(_FRED_SERIES, None)

    out: dict[str, tuple[float, ...] | None] = {}
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
    series: dict[str, tuple[float, ...] | None] = dict.fromkeys(_FRED_SERIES, None)
    if fred_api_key:
        series = await _fred_bundle(fred_api_key)
    else:
        logger.warning("macro: FRED_API_KEY not set — VIX/10y/DXY unavailable")

    def _latest(sid: str) -> float | None:
        hist = series.get(sid)
        return hist[0] if hist else None

    vix = _latest("VIXCLS")
    ten_year = _latest("DGS10")
    dxy = _latest("DTWEXBGS")

    return {
        "vix_level": round(vix, 1) if vix is not None else None,
        "ten_year_yield_pct": round(ten_year, 2) if ten_year is not None else None,
        # The macro prompt asks whether rates are "rising rapidly". A level
        # alone cannot answer that; this can.
        "ten_year_change_63d_bp": change_bp(series.get("DGS10"), _RATE_CHANGE_WINDOW),
        "dxy_index": round(dxy, 1) if dxy is not None else None,
        # Scale-free "is the dollar strong for its own recent history".
        "dxy_zscore_1y": zscore_latest(
            series.get("DTWEXBGS"), _ZSCORE_WINDOW, min_obs=_ZSCORE_MIN_OBS
        ),
        "sector_relative_strength": sector_relative_strength(symbol_bars, spy_bars),
    }
