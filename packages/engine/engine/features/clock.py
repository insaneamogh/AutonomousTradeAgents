"""Alpaca market clock (``/v2/clock``) — the authoritative session gate.

``market_calendar.is_us_market_open`` answers this from a hardcoded holiday
tuple plus, when installed, ``pandas_market_calendars``. That is correct
almost always, and "almost" is the problem: it cannot know about an
unscheduled early close (a national day of mourning, a weather close), and
the hardcoded holiday list silently goes stale the moment the calendar year
rolls past whatever was baked in. Both failure modes are invisible — the
scanner just quietly scans a closed market, or skips an open one.

The broker's own clock has no such gap: it is the same clock that decides
whether an order will be accepted. So the resolution order is:

  1. Alpaca ``/v2/clock``, cached briefly (the answer changes twice a day).
  2. On any failure — no keys, network, non-200 — the local calendar.

Fallback is silent-by-design in the sense that the scan still runs, but it
is *reported*: ``MarketClock.source`` says which answer you got, so a run
that fell back is distinguishable in a log from one that did not.

Deliberately NOT wired into the risk path. Session state is a scheduling
question ("should we look at the tape right now"), not a risk veto, and
the risk engine stays free of network calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol, runtime_checkable

from engine.features.market_calendar import is_us_market_open

logger = logging.getLogger("engine.features.clock")

CLOCK_TTL_SECONDS = 60.0
"""Cache window. The open/closed answer changes twice a day; a minute of
staleness is irrelevant and it keeps a 5-minute scanner off the endpoint."""


@dataclass(frozen=True)
class MarketClock:
    """Session state plus where the answer came from."""

    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None
    source: str = "local_calendar"
    """``alpaca`` when the broker answered, ``local_calendar`` on fallback."""


@runtime_checkable
class ClockProvider(Protocol):
    """What a caller (the scanner) needs from a clock — real or fake.

    Same shape as ``BarsProvider``/``IntradayBarsProvider`` in
    ``engine.features.bars``: a ``name`` for logging plus one async method,
    so tests can inject a trivial double without a network.
    """

    name: str

    async def now(self, *, at: datetime | None = None) -> MarketClock: ...


class AlpacaClock:
    """``/v2/clock`` with a TTL cache and a local-calendar fallback."""

    name = "alpaca-clock"

    def __init__(self, api_key: str, secret_key: str, *, base_url: str | None = None) -> None:
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self._base = (base_url or "https://paper-api.alpaca.markets").rstrip("/")
        self._cached: MarketClock | None = None
        self._cached_at = 0.0

    async def now(self, *, at: datetime | None = None) -> MarketClock:
        """Current session state. ``at`` only affects the fallback path."""
        if self._cached is not None and monotonic() - self._cached_at < CLOCK_TTL_SECONDS:
            return self._cached

        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base}/v2/clock", headers=self._headers)
                resp.raise_for_status()
                clock = _clock_from_payload(resp.json())
        except Exception as exc:
            logger.warning(
                "clock: Alpaca /v2/clock unavailable (%s) — falling back to the "
                "local calendar. An unscheduled early close will be missed.",
                exc,
            )
            return _local(at)

        self._cached = clock
        self._cached_at = monotonic()
        return clock


def _clock_from_payload(payload: dict[str, Any]) -> MarketClock:
    """Parse the clock response. Split out so it is testable without a network."""
    return MarketClock(
        is_open=bool(payload.get("is_open", False)),
        next_open=_dt(payload.get("next_open")),
        next_close=_dt(payload.get("next_close")),
        source="alpaca",
    )


def _dt(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).astimezone(UTC)
    except ValueError:
        return None


def _local(at: datetime | None) -> MarketClock:
    now = at or datetime.now(UTC)
    return MarketClock(is_open=is_us_market_open(now), source="local_calendar")


def clock_from_env() -> AlpacaClock | None:
    """Alpaca-backed clock when trading keys are set; otherwise None.

    Reads ``ALPACA_BASE_URL`` so a paper deployment asks the paper clock and
    a live one asks live. They agree today, but pointing at the wrong host
    is the kind of thing that only stops agreeing at the worst moment.
    """
    import os

    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = (
        os.environ.get("ALPACA_SECRET_KEY", "").strip()
        or os.environ.get("ALPACA_API_SECRET", "").strip()
    )
    if not api_key or not secret:
        return None
    return AlpacaClock(api_key, secret, base_url=os.environ.get("ALPACA_BASE_URL") or None)
