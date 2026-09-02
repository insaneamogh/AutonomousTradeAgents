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

``resolve_market_clock`` (``docs/PLAN_ALPACA_MCP.md`` D.3) adds a third,
outermost link ahead of the two above — Alpaca's own ``alpaca`` CLI binary,
gated behind ``USE_ALPACA_CLI`` (default off) — so the hackathon's "use
Alpaca's own MCP server or CLI" requirement has a real, judge-visible
artifact (``market_open_source: "alpaca_cli"``) without changing what
happens when the flag is off.
"""

from __future__ import annotations

import logging
import os
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
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = (
        os.environ.get("ALPACA_SECRET_KEY", "").strip()
        or os.environ.get("ALPACA_API_SECRET", "").strip()
    )
    if not api_key or not secret:
        return None
    return AlpacaClock(api_key, secret, base_url=os.environ.get("ALPACA_BASE_URL") or None)


def use_alpaca_cli() -> bool:
    """Whether ``resolve_market_clock``'s CLI step is enabled.

    **Default ON as of 2026-09-02.** It defaulted OFF when the binary was
    first added to the image, so that the image change and the behaviour
    change stayed independently revertible. That was right at the time and
    is wrong now: the hackathon requires using Alpaca's own MCP server or
    CLI, this CLI call is the project's eligibility artifact, and a
    flag-gated artifact that nobody remembered to flip is an artifact that
    never ran. Shipping the binary but not calling it satisfies nothing.

    Defaulting ON is safe because every failure mode already degrades
    rather than breaking: ``cli_clock()`` returns ``None`` on a missing
    binary, a non-zero exit, a timeout or unparseable JSON, and
    ``resolve_market_clock`` then falls through to Alpaca REST and finally
    the local calendar. A dev laptop with no ``alpaca`` installed takes
    exactly the path it took before, one ``logger.info`` louder.

    ``USE_ALPACA_CLI=0`` turns it off again with no deploy.
    """
    raw = os.environ.get("USE_ALPACA_CLI", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


async def resolve_market_clock(
    *, at: datetime | None = None, alpaca: ClockProvider | None = None
) -> MarketClock:
    """Three-step fallback chain, each link already reporting its own
    ``source``::

        CLI (only when USE_ALPACA_CLI=1)  ->  alpaca (REST)  ->  local calendar

    This function's only job is the ordering — every step beneath it
    already fails safe and never raises on its own account.
    ``cli_clock()`` (imported lazily below — see the note on the import)
    returns ``None`` on any failure, so a dev laptop with no ``alpaca``
    binary and ``USE_ALPACA_CLI`` unset falls straight through to
    ``alpaca`` without so much as a log line above debug.

    ``alpaca`` is typically an ``AlpacaClock`` from ``clock_from_env()``;
    when it is ``None`` (no trading keys configured), this goes straight to
    the local calendar — the exact same degrade path a ``Scanner`` with no
    clock injected at all already takes.
    """
    if use_alpaca_cli():
        # Deferred import: avoids a module-level clock.py <-> alpaca_cli.py
        # cycle (alpaca_cli.py imports MarketClock from this module), and
        # skips paying for the subprocess-adjacent import on the (default)
        # disabled path.
        from engine.features.alpaca_cli import cli_clock

        cli_result = await cli_clock()
        if cli_result is not None:
            return cli_result
        logger.info(
            "clock: USE_ALPACA_CLI=1 but the CLI step returned nothing — "
            "falling back to REST/local calendar"
        )

    if alpaca is not None:
        return await alpaca.now(at=at)
    return _local(at)


@dataclass
class ResolvingClock:
    """``ClockProvider`` that layers ``resolve_market_clock``'s CLI-first
    chain in front of a REST clock.

    This is the object ``resolved_clock_from_env()`` hands to
    ``scanner_from_env()`` as ``Scanner.clock`` — the thing that actually
    makes the CLI step reachable from a live scan, so
    ``market_open_source: "alpaca_cli"`` can reach the scanner-status API
    response once ``USE_ALPACA_CLI=1`` (the judge-visible eligibility
    artifact — ``docs/PLAN_ALPACA_MCP.md`` D.3).
    """

    name: str = "resolved-clock"
    alpaca: ClockProvider | None = None

    async def now(self, *, at: datetime | None = None) -> MarketClock:
        return await resolve_market_clock(at=at, alpaca=self.alpaca)


def resolved_clock_from_env() -> ClockProvider:
    """The env-wired ``ClockProvider`` for the scanner: the CLI step (when
    ``USE_ALPACA_CLI=1``) in front of ``clock_from_env()``'s REST clock.

    With the default ``USE_ALPACA_CLI=0`` this behaves exactly like handing
    ``clock_from_env()``'s result straight to the scanner — D.3 adds a link
    to the chain, it does not change what the existing links do.
    """
    return ResolvingClock(alpaca=clock_from_env())
