"""Earnings calendar -> days_to_earnings -> earnings_blackout can fire.

`options_earnings_blackout_days` existed from the start and never refused
anything: days_to_earnings was hardcoded None. These pin the source, the
parsing, and the plumbing into the options context block.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

from engine.features import earnings as earnings_mod
from engine.features.earnings import FinnhubEarningsCalendar, next_on_or_after
from engine.features.provider import MinimalOptionsContextProvider

TODAY = date(2026, 9, 23)


def test_the_earliest_upcoming_print_wins() -> None:
    payload = {"earningsCalendar": [
        {"date": "2026-10-28", "symbol": "NVDA"},
        {"date": "2026-09-01", "symbol": "NVDA"},  # past
        {"date": "2026-10-02", "symbol": "NVDA"},
        {"date": "2026-09-25", "symbol": "AMD"},   # other symbol
        {"date": "not-a-date", "symbol": "NVDA"},
    ]}
    assert next_on_or_after(payload, "NVDA", TODAY) == date(2026, 10, 2)


def test_no_rows_or_garbage_is_unknown_not_zero() -> None:
    assert next_on_or_after({"earningsCalendar": []}, "NVDA", TODAY) is None
    assert next_on_or_after({"error": "limit"}, "NVDA", TODAY) is None
    assert next_on_or_after(None, "NVDA", TODAY) is None


class _Stub:
    def __init__(self, handler, calls: list) -> None:
        self._handler, self._calls = handler, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, params=None, headers=None):
        self._calls.append((url, dict(params or {}), dict(headers or {})))
        return await self._handler()


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> list:
    calls: list = []
    monkeypatch.setattr(earnings_mod.httpx, "AsyncClient", lambda **_: _Stub(handler, calls))
    return calls


def test_the_key_travels_in_a_header_never_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok():
        return httpx.Response(200, json={"earningsCalendar": [{"date": "2026-10-02", "symbol": "NVDA"}]},
                              request=httpx.Request("GET", earnings_mod._FINNHUB_URL))

    calls = _install(monkeypatch, ok)
    cal = FinnhubEarningsCalendar("secret-token")
    assert asyncio.run(cal.next_earnings("nvda", TODAY)) == date(2026, 10, 2)
    url, params, headers = calls[0]
    assert headers["X-Finnhub-Token"] == "secret-token"
    assert "secret-token" not in url and "secret-token" not in str(params)


def test_a_symbol_day_is_fetched_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok():
        return httpx.Response(200, json={"earningsCalendar": []},
                              request=httpx.Request("GET", earnings_mod._FINNHUB_URL))

    calls = _install(monkeypatch, ok)
    cal = FinnhubEarningsCalendar("k")

    async def go():
        await cal.next_earnings("NVDA", TODAY)
        await cal.next_earnings("NVDA", TODAY)

    asyncio.run(go())
    assert len(calls) == 1


def test_an_outage_is_unknown_and_does_not_log_the_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom():
        raise httpx.ConnectTimeout("finnhub down")

    _install(monkeypatch, boom)
    with caplog.at_level("WARNING"):
        assert asyncio.run(FinnhubEarningsCalendar("secret-token").next_earnings("NVDA", TODAY)) is None
    assert "secret-token" not in caplog.text


class _FakeCalendar:
    name = "fake"

    def __init__(self, d: date | None) -> None:
        self._d = d

    async def next_earnings(self, symbol: str, today: date) -> date | None:
        return self._d


def test_the_context_block_carries_days_to_earnings(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    block = asyncio.run(
        MinimalOptionsContextProvider(earnings=_FakeCalendar(today + timedelta(days=1))).fetch("NVDA")
    )
    assert block["days_to_earnings"] == 1
    assert asyncio.run(MinimalOptionsContextProvider().fetch("NVDA"))["days_to_earnings"] is None


def test_the_feed_badge_follows_the_feed_actually_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker picks OPRA from ALPACA_OPTIONS_FEED. The badge said
    "15 min delayed" unconditionally, so after the upgrade it would lie."""
    monkeypatch.delenv("ALPACA_OPTIONS_FEED", raising=False)
    block = asyncio.run(MinimalOptionsContextProvider().fetch("NVDA"))
    assert (block["data_delay_minutes"], block["feed_type"]) == (15, "indicative_delayed")
    monkeypatch.setenv("ALPACA_OPTIONS_FEED", "opra")
    block = asyncio.run(MinimalOptionsContextProvider().fetch("NVDA"))
    assert (block["data_delay_minutes"], block["feed_type"]) == (0, "opra_realtime")
