"""Next earnings date per symbol, so `earnings_blackout` can actually fire.

`options_earnings_blackout_days` (2) has been a rule since the options
path shipped, and it has never refused anything: `days_to_earnings` was
hardcoded None because Alpaca publishes no earnings calendar
(`corporate_actions.py` says so). A long option held into a print pays for
the IV crush the day after, whichever way the stock goes.

Source: Finnhub's free-tier earnings calendar,
`GET /api/v1/calendar/earnings?from=&to=&symbol=`, returning
`{"earningsCalendar": [{"date": "YYYY-MM-DD", "hour": "bmo|amc|dmh", ...}]}`.
The key rides in the `X-Finnhub-Token` header, never the query string, so
it cannot leak through a logged URL (the same failure macro.py guards
against for FRED).

Contract, same as every optional feature block here: never raises, never
blocks the council for long, and returns None on no key, no data or
any error. None means "unknown", and the blackout rule self-gates on
unknown rather than guessing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date, timedelta
from typing import Protocol

import httpx

logger = logging.getLogger("engine.features.earnings")

_FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
_TIMEOUT_S = 5.0
_LOOKAHEAD_DAYS = 120
"""Far enough to see the next quarterly print from any point in a quarter."""
_FAILURE_TTL_S = 300.0


class EarningsCalendar(Protocol):
    name: str

    async def next_earnings(self, symbol: str, today: date) -> date | None: ...


class FinnhubEarningsCalendar:
    name = "finnhub-earnings"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        # (symbol, today) -> (next date or None, monotonic expiry)
        self._cache: dict[tuple[str, date], tuple[date | None, float]] = {}

    async def next_earnings(self, symbol: str, today: date) -> date | None:
        sym = symbol.upper()
        key = (sym, today)
        hit = self._cache.get(key)
        if hit is not None and hit[1] > time.monotonic():
            return hit[0]

        params = {
            "from": today.isoformat(),
            "to": (today + timedelta(days=_LOOKAHEAD_DAYS)).isoformat(),
            "symbol": sym,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.get(
                    _FINNHUB_URL, params=params, headers={"X-Finnhub-Token": self._api_key}
                )
                resp.raise_for_status()
                payload = resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f"HTTP {exc.response.status_code}"
            logger.warning("earnings: Finnhub fetch failed for %s — %s", sym, detail)
            self._cache[key] = (None, time.monotonic() + _FAILURE_TTL_S)
            return None

        nxt = next_on_or_after(payload, sym, today)
        # A found date is good for the UTC day. So is a confirmed "none in
        # the window": it will not change intraday.
        self._cache[key] = (nxt, time.monotonic() + 86_400.0)
        return nxt


def next_on_or_after(payload: object, symbol: str, today: date) -> date | None:
    """Earliest calendar date for `symbol` on or after `today`. Pure; the
    parsing is split out so it is testable without a network."""
    rows = payload.get("earningsCalendar") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    dates: list[date] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")).upper() not in ("", symbol.upper()):
            continue
        try:
            d = date.fromisoformat(str(row.get("date", "")))
        except ValueError:
            continue
        if d >= today:
            dates.append(d)
    return min(dates) if dates else None


def earnings_calendar_from_env() -> EarningsCalendar | None:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    return FinnhubEarningsCalendar(key) if key else None
