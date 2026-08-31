"""Coverage for the ticker typeahead: ranking, caching, and assert_tradable.

``symbol_search._universe()`` and ``assert_tradable()`` both do lazy, LOCAL
imports of ``list_tradable_assets`` / ``lookup_asset`` from ``broker.alpaca``
inside the function body, so the fetch/lookup calls must be monkeypatched on
``broker.alpaca`` itself — patching a path under ``app.services...`` would
have no effect, since that's not where the lookup happens at call time.

``symbol_search._cache`` / ``_cached_at`` are module-level globals with no
test-reset helper (deliberately — the module is left unmodified), so every
test resets them directly via monkeypatch.

Importing ``broker.alpaca`` pulls in ``alpaca.trading``, whose own
``trading/stream.py`` still imports ``websockets.legacy`` — deprecated by
the ``websockets`` version this workspace resolves to. That's a pre-existing
alpaca-py/websockets version mismatch, unrelated to this module; nothing
here previously imported ``broker.alpaca`` for real (every existing caller
either has no keys configured or hits it lazily), so this is the first test
to trigger it. This repo's ``filterwarnings = ["error"]`` would otherwise
turn that DeprecationWarning into a collection-time crash, so it's
suppressed locally, right around the one import that triggers it.
"""

from __future__ import annotations

import os
import time
import warnings
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    import broker.alpaca as alpaca_module
    from broker.alpaca import AssetInfo

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app
from app.services.auth.auth_store import reset_auth_store_for_tests
from app.services.broker import symbol_search as ss
from app.services.watchlist.watchlist_store import reset_watchlist_store_for_tests


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_auth_store_for_tests()
    reset_watchlist_store_for_tests()
    # The cache starts cold and un-keyed for every test; individual tests
    # opt into a warm cache or real keys as needed.
    monkeypatch.setattr(ss, "_cache", [])
    monkeypatch.setattr(ss, "_cached_at", 0.0)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _seed_cache(monkeypatch: pytest.MonkeyPatch, assets: list[AssetInfo]) -> None:
    """Warm the module cache directly with SymbolHit rows, bypassing fetch."""
    hits = [ss.SymbolHit(symbol=a.symbol, name=a.name, fractionable=a.fractionable) for a in assets]
    monkeypatch.setattr(ss, "_cache", hits)
    monkeypatch.setattr(ss, "_cached_at", time.monotonic())


# A small fixture universe built to exercise every rank tier at once,
# using the module's own worked example: "apple" should surface Apple
# Inc. via its name, ahead of a leveraged ETF that merely mentions Apple.
UNIVERSE = [
    AssetInfo(symbol="AAPL", name="Apple Inc.", tradable=True, fractionable=True),
    AssetInfo(symbol="AAPLW", name="Apple Inc Warrants", tradable=True, fractionable=False),
    AssetInfo(
        symbol="APLU",
        name="T-Rex 2X Long Apple Daily Target ETF",
        tradable=True,
        fractionable=True,
    ),
    AssetInfo(symbol="MSFT", name="Microsoft Corporation", tradable=True, fractionable=True),
    AssetInfo(symbol="GOOGL", name="Alphabet Inc Class A", tradable=True, fractionable=True),
]


# ── Ranking ──────────────────────────────────────────────────────────


async def test_exact_ticker_beats_prefix_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_cache(monkeypatch, UNIVERSE)
    hits = await ss.search_symbols("AAPL")
    symbols = [h.symbol for h in hits]
    assert symbols[0] == "AAPL"  # exact match, rank 0
    assert "AAPLW" in symbols  # prefix match, rank 1 — still present, just lower
    assert symbols.index("AAPL") < symbols.index("AAPLW")


async def test_name_start_beats_word_boundary_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module's own "apple" example, pinned: real Apple over the leveraged ETF."""
    _seed_cache(monkeypatch, UNIVERSE)
    hits = await ss.search_symbols("apple")
    symbols = [h.symbol for h in hits]
    assert symbols[0] == "AAPL", f"Apple Inc. should rank first for 'apple', got {symbols}"
    assert symbols.index("AAPL") < symbols.index("APLU")


async def test_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_cache(monkeypatch, UNIVERSE)
    lower = [h.symbol for h in await ss.search_symbols("aapl")]
    upper = [h.symbol for h in await ss.search_symbols("AAPL")]
    mixed = [h.symbol for h in await ss.search_symbols("AaPl")]
    assert lower == upper == mixed
    assert lower[0] == "AAPL"


async def test_no_match_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_cache(monkeypatch, UNIVERSE)
    hits = await ss.search_symbols("ZZZNOPE")
    assert hits == []


async def test_limit_truncates_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_cache(monkeypatch, UNIVERSE)
    hits = await ss.search_symbols("apple", limit=1)
    assert len(hits) == 1
    assert hits[0].symbol == "AAPL"


@pytest.mark.parametrize("query", ["", "   "])
async def test_empty_query_returns_empty(monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    _seed_cache(monkeypatch, UNIVERSE)
    assert await ss.search_symbols(query) == []


# ── Caching ──────────────────────────────────────────────────────────


async def test_fetch_happens_once_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    calls = {"n": 0}

    async def fake_fetch(*, api_key: str, secret_key: str) -> list[AssetInfo]:
        calls["n"] += 1
        return [AssetInfo(symbol="AAPL", name="Apple Inc.", tradable=True, fractionable=True)]

    monkeypatch.setattr(alpaca_module, "list_tradable_assets", fake_fetch)

    await ss.search_symbols("AAPL")
    await ss.search_symbols("AAPL")
    await ss.warm_symbol_cache()

    assert calls["n"] == 1


async def test_no_keys_returns_empty_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    # No ALPACA_API_KEY / ALPACA_SECRET_KEY set — the autouse fixture already
    # deleted them, so this is the deployment's default state.
    calls = {"n": 0}

    async def fake_fetch(*, api_key: str, secret_key: str) -> list[AssetInfo]:
        calls["n"] += 1
        return list(UNIVERSE)

    monkeypatch.setattr(alpaca_module, "list_tradable_assets", fake_fetch)

    assert await ss.search_symbols("AAPL") == []
    assert calls["n"] == 0


async def test_fetch_exception_serves_stale_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    calls = {"n": 0}

    async def flaky_fetch(*, api_key: str, secret_key: str) -> list[AssetInfo]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [AssetInfo(symbol="AAPL", name="Apple Inc.", tradable=True, fractionable=True)]
        raise RuntimeError("alpaca is down")

    monkeypatch.setattr(alpaca_module, "list_tradable_assets", flaky_fetch)

    first = [h.symbol for h in await ss.search_symbols("AAPL")]
    assert first == ["AAPL"]

    # Force the TTL to have elapsed so the next call attempts a refresh —
    # which the flaky fetch above will fail on its second call.
    monkeypatch.setattr(ss, "_cached_at", time.monotonic() - ss._TTL_SECONDS - 1)

    second = [h.symbol for h in await ss.search_symbols("AAPL")]
    assert second == ["AAPL"], "a failed refresh must still serve the stale cache"
    assert calls["n"] == 2, "the refresh must actually have been attempted"


# ── assert_tradable ──────────────────────────────────────────────────


async def test_assert_tradable_noop_without_keys() -> None:
    calls = {"n": 0}

    async def fake_lookup(symbol: str, *, api_key: str, secret_key: str) -> AssetInfo | None:
        calls["n"] += 1
        return None

    # Not monkeypatched onto alpaca_module here on purpose: with no keys,
    # assert_tradable must return before ever importing/calling lookup_asset.
    await ss.assert_tradable("ZZZZZ")
    assert calls["n"] == 0


async def test_assert_tradable_422_when_symbol_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    async def fake_lookup(symbol: str, *, api_key: str, secret_key: str) -> AssetInfo | None:
        return None

    monkeypatch.setattr(alpaca_module, "lookup_asset", fake_lookup)

    with pytest.raises(HTTPException) as exc_info:
        await ss.assert_tradable("ZZZZZ")
    assert exc_info.value.status_code == 422
    assert "ZZZZZ" in str(exc_info.value.detail)


async def test_assert_tradable_422_when_not_tradable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    async def fake_lookup(symbol: str, *, api_key: str, secret_key: str) -> AssetInfo | None:
        return AssetInfo(symbol="HALTD", name="Halted Co", tradable=False, fractionable=False)

    monkeypatch.setattr(alpaca_module, "lookup_asset", fake_lookup)

    with pytest.raises(HTTPException) as exc_info:
        await ss.assert_tradable("HALTD")
    assert exc_info.value.status_code == 422
    assert "HALTD" in str(exc_info.value.detail)


async def test_assert_tradable_passes_when_tradable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    async def fake_lookup(symbol: str, *, api_key: str, secret_key: str) -> AssetInfo | None:
        return AssetInfo(symbol="AAPL", name="Apple Inc.", tradable=True, fractionable=True)

    monkeypatch.setattr(alpaca_module, "lookup_asset", fake_lookup)

    await ss.assert_tradable("AAPL")  # must not raise


async def test_assert_tradable_fails_open_when_lookup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    async def broken_lookup(symbol: str, *, api_key: str, secret_key: str) -> AssetInfo | None:
        raise RuntimeError("broker hiccup")

    monkeypatch.setattr(alpaca_module, "lookup_asset", broken_lookup)

    # A broker outage must not block trading — the symbol is allowed through.
    await ss.assert_tradable("AAPL")


# ── Router smoke test ────────────────────────────────────────────────


def test_add_nonexistent_symbol_is_422_via_assert_tradable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First router-level test of POST /watchlist: a shape-valid but
    non-existent ticker must be rejected by the live assert_tradable
    check, not just wave through on the SYMBOL_RE shape regex.

    POST /watchlist requires require_real_auth (docs/IMPL_DEMO_SESSION.md
    §3 — it changes what the agent trades, so the dev-bypass fixture user
    is refused here too, not just a demo session), so this needs a real
    logged-in Bearer token rather than the bypass.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    async def fake_lookup(symbol: str, *, api_key: str, secret_key: str) -> AssetInfo | None:
        return None

    monkeypatch.setattr(alpaca_module, "lookup_asset", fake_lookup)

    email = "symbol-search-watchlist@example.com"
    challenge = client.post("/api/v1/auth/request-login", json={"email": email}).json()
    verified = client.post(
        "/api/v1/auth/verify", json={"email": email, "token": challenge["devToken"]}
    ).json()
    headers = {"Authorization": f"Bearer {verified['accessToken']}"}

    r = client.post("/api/v1/watchlist", json={"symbol": "ZZZZZ"}, headers=headers)
    assert r.status_code == 422
    assert "ZZZZZ" in r.json()["detail"]
