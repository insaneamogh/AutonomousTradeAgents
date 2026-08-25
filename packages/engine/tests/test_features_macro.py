"""FRED macro block: parsing, caching, and outage behaviour.

The council must never fail or stall because FRED is slow or down — macro
is context for a prompt, not a risk gate.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from engine.features import macro
from engine.features.macro import compute_macro, fred_latest, reset_fred_cache


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    reset_fred_cache()


class _StubClient:
    """Stands in for ``httpx.AsyncClient`` inside ``fred_latest``."""

    def __init__(self, handler, calls: list[str]) -> None:
        self._handler = handler
        self._calls = calls

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, url: str, params: dict) -> httpx.Response:
        self._calls.append(params["series_id"])
        return await self._handler(params)


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(macro.httpx, "AsyncClient", lambda **_: _StubClient(handler, calls))
    return calls


def _json(observations: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"observations": observations},
        request=httpx.Request("GET", macro._FRED_URL),
    )


def test_missing_holiday_values_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRED writes '.' on non-publication days; take the first real value."""

    async def handler(_: dict) -> httpx.Response:
        return _json(
            [
                {"date": "2026-08-25", "value": "."},
                {"date": "2026-08-24", "value": "."},
                {"date": "2026-08-23", "value": "15.13"},
            ]
        )

    _install(monkeypatch, handler)
    assert asyncio.run(fred_latest("VIXCLS", "k")) == 15.13


def test_all_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(_: dict) -> httpx.Response:
        return _json([{"date": "2026-08-25", "value": "."}])

    _install(monkeypatch, handler)
    assert asyncio.run(fred_latest("DGS10", "k")) is None


def test_success_is_cached_for_the_day(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(_: dict) -> httpx.Response:
        return _json([{"date": "2026-08-25", "value": "4.74"}])

    calls = _install(monkeypatch, handler)

    async def go() -> None:
        assert await fred_latest("DGS10", "k") == 4.74
        assert await fred_latest("DGS10", "k") == 4.74

    asyncio.run(go())
    assert calls == ["DGS10"], "second read must come from the in-process cache"


def test_failure_is_negatively_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """An outage costs one timeout per series per TTL, not one per symbol."""

    async def handler(_: dict) -> httpx.Response:
        raise httpx.ConnectTimeout("fred is down")

    calls = _install(monkeypatch, handler)

    async def go() -> None:
        assert await fred_latest("VIXCLS", "k") is None
        assert await fred_latest("VIXCLS", "k") is None

    asyncio.run(go())
    assert calls == ["VIXCLS"]


def test_api_key_never_reaches_the_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """httpx puts the full URL — including api_key — in its error message."""
    secret = "supersecretfredkey"

    async def handler(params: dict) -> httpx.Response:
        req = httpx.Request("GET", f"{macro._FRED_URL}?api_key={params['api_key']}")
        raise httpx.HTTPStatusError(
            f"400 Bad Request for url '{req.url}'",
            request=req,
            response=httpx.Response(400, request=req),
        )

    _install(monkeypatch, handler)
    with caplog.at_level("WARNING"):
        assert asyncio.run(fred_latest("VIXCLS", secret)) is None
    assert secret not in caplog.text
    assert "HTTP 400" in caplog.text


def test_compute_macro_survives_a_total_fred_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The council keeps running; every series just renders n/a."""

    async def handler(_: dict) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _install(monkeypatch, handler)
    out = asyncio.run(compute_macro(fred_api_key="k", symbol_bars=[], spy_bars=[]))
    assert out == {
        "vix_level": None,
        "ten_year_yield_pct": None,
        "dxy_index": None,
        "sector_relative_strength": None,
    }


def test_compute_macro_gives_up_on_a_hung_fred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FRED that accepts the connection and never answers must not stall
    the council past the wall-clock budget."""

    async def handler(_: dict) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    _install(monkeypatch, handler)
    monkeypatch.setattr(macro, "_FRED_BUDGET_S", 0.2)

    async def go() -> dict:
        loop = asyncio.get_running_loop()
        started = loop.time()
        out = await compute_macro(fred_api_key="k", symbol_bars=[], spy_bars=[])
        assert loop.time() - started < 2.0
        return out

    out = asyncio.run(go())
    assert out["vix_level"] is None


def test_no_api_key_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOCK mode: no key, no network call, still a well-formed block."""

    async def handler(_: dict) -> httpx.Response:
        raise AssertionError("must not hit the network without a key")

    calls = _install(monkeypatch, handler)
    out = asyncio.run(compute_macro(fred_api_key=None, symbol_bars=[], spy_bars=[]))
    assert calls == []
    assert set(out) == {
        "vix_level",
        "ten_year_yield_pct",
        "dxy_index",
        "sector_relative_strength",
    }


def test_series_are_fetched_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three serial 6s timeouts would be an 18s per-symbol stall."""
    inflight = 0
    peak = 0

    async def handler(params: dict) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return _json([{"date": "2026-08-25", "value": "1.0"}])

    _install(monkeypatch, handler)
    asyncio.run(compute_macro(fred_api_key="k", symbol_bars=[], spy_bars=[]))
    assert peak == len(macro._FRED_SERIES)
