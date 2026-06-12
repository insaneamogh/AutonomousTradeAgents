"""Macro features — FRED series + sector relative strength.

FRED (https://fred.stlouisfed.org) is free: set ``FRED_API_KEY``. Series:

    VIXCLS     CBOE VIX close           → ``vix_level``
    DGS10      10-year Treasury yield   → ``ten_year_yield_pct``
    DTWEXBGS   Broad dollar index       → ``dxy_index``

Values are published with up to a 1-business-day lag — fine for a
daily-bar swing product. One fetch per (series, UTC day) is cached
in-process.

``sector_relative_strength`` is computed from bars, not FRED: the symbol's
21-day return minus SPY's 21-day return, in percentage points — the same
definition the synthetic provider faked.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import httpx

from engine.features.technicals import DailyBar

logger = logging.getLogger("engine.features.macro")

_FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# (series_id, utc_date) → latest value
_fred_cache: dict[tuple[str, date], float] = {}


async def fred_latest(series_id: str, api_key: str) -> float | None:
    """Most recent non-missing observation for a FRED series, or None."""
    today = datetime.now(UTC).date()
    cache_key = (series_id, today)
    if cache_key in _fred_cache:
        return _fred_cache[cache_key]

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 10,  # skip trailing '.' (missing) observations
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_FRED_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception:  # noqa: BLE001
        logger.exception("macro: FRED fetch failed for %s", series_id)
        return None

    for obs in payload.get("observations", []):
        raw = obs.get("value", ".")
        if raw not in (".", "", None):
            try:
                value = float(raw)
            except ValueError:
                continue
            _fred_cache[cache_key] = value
            return value
    logger.warning("macro: no usable observation for FRED %s", series_id)
    return None


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
) -> dict:
    """The council's ``macro`` feature block. Missing series stay None —
    prompts render 'n/a' and the Macro Analyst reasons with what exists."""
    vix = ten_year = dxy = None
    if fred_api_key:
        vix = await fred_latest("VIXCLS", fred_api_key)
        ten_year = await fred_latest("DGS10", fred_api_key)
        dxy = await fred_latest("DTWEXBGS", fred_api_key)
    else:
        logger.warning("macro: FRED_API_KEY not set — VIX/10y/DXY unavailable")

    return {
        "vix_level": round(vix, 1) if vix is not None else None,
        "ten_year_yield_pct": round(ten_year, 2) if ten_year is not None else None,
        "dxy_index": round(dxy, 1) if dxy is not None else None,
        "sector_relative_strength": sector_relative_strength(symbol_bars, spy_bars),
    }
