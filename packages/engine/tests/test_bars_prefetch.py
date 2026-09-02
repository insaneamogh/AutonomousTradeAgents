"""Batched daily-bar prefetch — what makes a 1000-symbol scan affordable.

``daily_bars`` is one HTTP request per symbol. Alpaca's StockBarsRequest
accepts a list, so the same 150 symbols cost 2 requests instead of 150.
These tests pin the three properties that make that safe to rely on:
it writes into the cache ``daily_bars`` reads, it never raises, and a
failure degrades to the old per-symbol path rather than losing data.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

# Same websockets.legacy guard as test_options_contracts.py — importing
# alpaca-py pulls in a deprecated transitive module, and this suite runs
# under `filterwarnings = ["error"]`. Imported here at module scope so the
# warning fires during collection rather than inside whichever test
# happens to touch alpaca first.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    import alpaca.data.requests  # noqa: F401

from engine.features.bars import AlpacaDailyBarsProvider


class _FakeBar:
    def __init__(self, day: datetime) -> None:
        self.timestamp = day
        self.open = self.high = self.low = self.close = 100.0
        self.volume = 1000.0


class _FakeClient:
    """Records every request so batching can be counted, not assumed."""

    def __init__(self, *, symbols_with_data: set[str] | None = None, raises: bool = False):
        self.requests: list[list[str]] = []
        self._symbols = symbols_with_data
        self._raises = raises

    def get_stock_bars(self, req):
        syms = req.symbol_or_symbols
        syms = [syms] if isinstance(syms, str) else list(syms)
        self.requests.append(syms)
        if self._raises:
            raise RuntimeError("simulated API failure")
        day = datetime(2026, 9, 1, tzinfo=UTC)
        have = self._symbols if self._symbols is not None else set(syms)
        return SimpleNamespace(
            data={s: [_FakeBar(day)] for s in syms if s in have}
        )


def _provider(client: _FakeClient) -> AlpacaDailyBarsProvider:
    p = AlpacaDailyBarsProvider("k", "s")
    p._client = client
    return p


@pytest.mark.asyncio
async def test_many_symbols_cost_a_few_requests_not_one_each() -> None:
    client = _FakeClient()
    provider = _provider(client)

    symbols = [f"SYM{i:03d}" for i in range(250)]
    fetched = await provider.prefetch_daily_bars(symbols, batch_size=100)

    assert fetched == 250
    assert len(client.requests) == 3, (
        "250 symbols at 100 per batch must be 3 requests — this ratio is the "
        "entire reason the method exists"
    )
    # Assert the request CONTENTS, not just the count. Counting alone
    # cannot tell batching from a loop that sends one symbol per request
    # and drops the other 99 — the request count is identical either way,
    # every symbol still gets cached (as empty), and `fetched` still reads
    # 250. Found by revert-checking: breaking `symbol_or_symbols` to a
    # single symbol left this test green.
    assert [len(r) for r in client.requests] == [100, 100, 50], (
        "each request must carry its WHOLE batch"
    )
    assert sorted(sym for req in client.requests for sym in req) == sorted(symbols), (
        "every requested symbol must appear in exactly one batch"
    )
    # And the bars must actually have arrived, not just a cache entry.
    assert await provider.daily_bars("SYM249"), "a batched symbol must have real bars"


@pytest.mark.asyncio
async def test_the_prefetch_populates_the_cache_daily_bars_reads() -> None:
    """The whole design: callers keep calling ``daily_bars`` per symbol and
    simply find it already there. If the cache keys did not line up, the
    prefetch would be a wasted round trip and every symbol would refetch."""
    client = _FakeClient()
    provider = _provider(client)

    await provider.prefetch_daily_bars(["AAPL", "MSFT"])
    before = len(client.requests)
    bars = await provider.daily_bars("AAPL")

    assert bars, "prefetched bars must be readable through the normal path"
    assert len(client.requests) == before, "daily_bars must not re-request"


@pytest.mark.asyncio
async def test_already_cached_symbols_are_not_refetched() -> None:
    client = _FakeClient()
    provider = _provider(client)

    await provider.daily_bars("AAPL")
    fetched = await provider.prefetch_daily_bars(["AAPL", "MSFT"])

    assert fetched == 1, "AAPL was already cached; only MSFT needed fetching"
    assert client.requests[-1] == ["MSFT"]


@pytest.mark.asyncio
async def test_a_failed_batch_degrades_to_the_per_symbol_path() -> None:
    """The only safe failure mode for an optimisation. A prefetch that
    could break a scan would be worse than no prefetch at all."""
    client = _FakeClient(raises=True)
    provider = _provider(client)

    fetched = await provider.prefetch_daily_bars(["AAPL", "MSFT"])

    assert fetched == 0
    assert provider._cache == {}, (
        "a failed batch must leave symbols UNCACHED so daily_bars refetches "
        "them individually — caching an empty result here would silently "
        "blank the scan"
    )


@pytest.mark.asyncio
async def test_a_symbol_with_no_bars_is_cached_as_empty() -> None:
    """Distinct from the failure case above. Alpaca genuinely having no
    bars for a symbol is a fact about today; re-asking every sweep is the
    waste this method exists to remove."""
    client = _FakeClient(symbols_with_data={"AAPL"})
    provider = _provider(client)

    await provider.prefetch_daily_bars(["AAPL", "DELISTED"])
    before = len(client.requests)
    bars = await provider.daily_bars("DELISTED")

    assert bars == []
    assert len(client.requests) == before, "an empty result must still be cached"


@pytest.mark.asyncio
async def test_an_empty_symbol_list_makes_no_request() -> None:
    client = _FakeClient()
    provider = _provider(client)
    assert await provider.prefetch_daily_bars([]) == 0
    assert client.requests == []


@pytest.mark.asyncio
async def test_duplicate_symbols_are_fetched_once() -> None:
    client = _FakeClient()
    provider = _provider(client)
    fetched = await provider.prefetch_daily_bars(["AAPL", "aapl", "AAPL"])
    assert fetched == 1
    assert client.requests == [["AAPL"]]
