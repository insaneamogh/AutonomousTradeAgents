"""lookup_asset / list_tradable_assets unit tests.

Both functions construct a fresh ``alpaca.trading.client.TradingClient`` on
every call (see the module docstring in ``broker/alpaca.py``), so the fake
is swapped in as the CLASS itself on ``alpaca.trading.client`` — patching
anything under ``broker.alpaca``'s own namespace would miss the local
``from alpaca.trading.client import TradingClient`` each function does
inside its body. Fixture rows are ``types.SimpleNamespace`` — the source
reads every field via ``getattr(..., default)``, so a real alpaca-py
Pydantic model is unnecessary.

Importing ``alpaca.trading.client`` pulls in ``alpaca.trading``'s own
``trading/stream.py``, which still imports ``websockets.legacy`` —
deprecated by the ``websockets`` version this workspace resolves to. That's
a pre-existing alpaca-py/websockets version mismatch, unrelated to this
module; this is the first test in this package to import ``broker.alpaca``
at all. This repo's ``filterwarnings = ["error"]`` would otherwise turn
that DeprecationWarning into a collection-time crash, so it's suppressed
locally, right around the one import that triggers it.
"""

from __future__ import annotations

import types
import warnings

import pytest

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    import alpaca.data.historical.screener as alpaca_screener_client
    import alpaca.trading.client as alpaca_trading_client

    from broker.alpaca import (
        AssetInfo,
        list_most_active_symbols,
        list_tradable_assets,
        lookup_asset,
    )


def _fake_client_class(
    *,
    get_asset_result: object = None,
    get_asset_raises: BaseException | None = None,
    get_all_assets_result: list[object] | None = None,
    received_symbols: list[str] | None = None,
):
    """Build a stand-in TradingClient class bound to the given behavior.

    A class, not an instance: `lookup_asset`/`list_tradable_assets` each do
    `TradingClient(api_key=..., secret_key=..., paper=True)` themselves, so
    a fresh instance is constructed per call. ``received_symbols``, when
    given, collects every symbol passed to `get_asset` across instances —
    the only way to observe that call once the instance itself is gone.
    """
    results = get_all_assets_result if get_all_assets_result is not None else []

    class FakeTradingClient:
        def __init__(self, **kwargs: object) -> None:
            self.init_kwargs = kwargs

        def get_asset(self, symbol: str) -> object:
            if received_symbols is not None:
                received_symbols.append(symbol)
            if get_asset_raises is not None:
                raise get_asset_raises
            return get_asset_result

        def get_all_assets(self, request: object) -> list[object]:
            self.last_request = request
            return results

    return FakeTradingClient


def _row(**overrides: object) -> types.SimpleNamespace:
    base: dict[str, object] = {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "tradable": True,
        "fractionable": True,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ── lookup_asset ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_asset_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row(shortable=True, easy_to_borrow=False)
    monkeypatch.setattr(
        alpaca_trading_client, "TradingClient", _fake_client_class(get_asset_result=row)
    )

    info = await lookup_asset("aapl", api_key="k", secret_key="s")

    assert info == AssetInfo(
        symbol="AAPL",
        name="Apple Inc.",
        tradable=True,
        fractionable=True,
        shortable=True,
        easy_to_borrow=False,
    )


@pytest.mark.asyncio
async def test_lookup_asset_uppercases_the_symbol_it_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[str] = []
    fake_cls = _fake_client_class(get_asset_result=_row(), received_symbols=received)
    monkeypatch.setattr(alpaca_trading_client, "TradingClient", fake_cls)

    await lookup_asset("aapl", api_key="k", secret_key="s")

    assert received == ["AAPL"]


@pytest.mark.asyncio
async def test_lookup_asset_returns_none_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        alpaca_trading_client, "TradingClient", _fake_client_class(get_asset_result=None)
    )

    assert await lookup_asset("ZZZZZ", api_key="k", secret_key="s") is None


@pytest.mark.asyncio
async def test_lookup_asset_returns_none_when_client_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        alpaca_trading_client,
        "TradingClient",
        _fake_client_class(get_asset_raises=RuntimeError("boom")),
    )

    assert await lookup_asset("AAPL", api_key="k", secret_key="s") is None


# ── list_tradable_assets ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tradable_assets_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row(
            symbol="AAPL", name="Apple Inc.", fractionable=True, shortable=True, easy_to_borrow=True
        )
    ]
    monkeypatch.setattr(
        alpaca_trading_client, "TradingClient", _fake_client_class(get_all_assets_result=rows)
    )

    assets = await list_tradable_assets(api_key="k", secret_key="s")

    assert assets == [
        AssetInfo(
            symbol="AAPL",
            name="Apple Inc.",
            tradable=True,
            fractionable=True,
            shortable=True,
            easy_to_borrow=True,
        )
    ]


@pytest.mark.asyncio
async def test_list_tradable_assets_rechecks_tradable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request already filters status=ACTIVE, but the code re-checks
    ``tradable`` itself before including a row — pin that a row reporting
    tradable=False is dropped even if the broker's response included it."""
    rows = [
        _row(symbol="AAPL", tradable=True),
        _row(symbol="HALTD", name="Halted Co", tradable=False),
    ]
    monkeypatch.setattr(
        alpaca_trading_client, "TradingClient", _fake_client_class(get_all_assets_result=rows)
    )

    assets = await list_tradable_assets(api_key="k", secret_key="s")

    assert [a.symbol for a in assets] == ["AAPL"]
    assert assets[0].tradable is True


# ── has_options (attributes list) ────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_asset_reads_has_options_off_the_attributes_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(attributes=["has_options", "fractional_eh_enabled"])
    monkeypatch.setattr(
        alpaca_trading_client, "TradingClient", _fake_client_class(get_asset_result=row)
    )

    info = await lookup_asset("AAPL", api_key="k", secret_key="s")

    assert info is not None
    assert info.has_options is True


@pytest.mark.asyncio
async def test_lookup_asset_has_options_false_when_attributes_absent_or_missing_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No `attributes` key at all (row built without one) AND an empty list
    # both mean "not options-eligible" -- neither should raise or default
    # to True.
    for row in (_row(), _row(attributes=[])):
        monkeypatch.setattr(
            alpaca_trading_client, "TradingClient", _fake_client_class(get_asset_result=row)
        )
        info = await lookup_asset("AAPL", api_key="k", secret_key="s")
        assert info is not None
        assert info.has_options is False


@pytest.mark.asyncio
async def test_list_tradable_assets_reads_has_options_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _row(symbol="AAPL", attributes=["has_options"]),
        _row(symbol="TINY", attributes=[]),
    ]
    monkeypatch.setattr(
        alpaca_trading_client, "TradingClient", _fake_client_class(get_all_assets_result=rows)
    )

    assets = await list_tradable_assets(api_key="k", secret_key="s")

    by_symbol = {a.symbol: a.has_options for a in assets}
    assert by_symbol == {"AAPL": True, "TINY": False}


# ── list_most_active_symbols ─────────────────────────────────────────


def _fake_screener_client_class(*, by_volume: list[str], by_trades: list[str]):
    """Two independent rankings, keyed by the request's own `by` value —
    real Alpaca behavior: volume-rank and trade-count-rank are genuinely
    different orderings, not the same list twice."""
    from alpaca.data.enums import MostActivesBy

    results = {
        MostActivesBy.VOLUME: [types.SimpleNamespace(symbol=s) for s in by_volume],
        MostActivesBy.TRADES: [types.SimpleNamespace(symbol=s) for s in by_trades],
    }

    class FakeScreenerClient:
        def __init__(self, **kwargs: object) -> None:
            self.init_kwargs = kwargs

        def get_most_actives(self, request: object) -> object:
            return types.SimpleNamespace(most_actives=results[request.by])

    return FakeScreenerClient


@pytest.mark.asyncio
async def test_list_most_active_symbols_merges_both_rankings_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        alpaca_screener_client,
        "ScreenerClient",
        _fake_screener_client_class(
            by_volume=["NVDA", "TSLA", "AAPL"],
            by_trades=["AAPL", "MSFT"],  # AAPL overlaps -- must not duplicate
        ),
    )

    symbols = await list_most_active_symbols(api_key="k", secret_key="s")

    assert symbols == ["NVDA", "TSLA", "AAPL", "MSFT"], (
        "volume ranking first (in its own order), then any NEW symbols "
        "trade-count ranking adds, with no duplicate for the AAPL overlap"
    )


@pytest.mark.asyncio
async def test_list_most_active_symbols_clamps_top_to_alpacas_real_100_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alpaca's screener rejects `top > 100` outright (confirmed live:
    "invalid top: should not be larger than 100") -- verified against the
    real endpoint, not assumed. A caller passing a larger pool size must
    not crash the request."""
    fake_cls = _fake_screener_client_class(by_volume=[], by_trades=[])
    monkeypatch.setattr(alpaca_screener_client, "ScreenerClient", fake_cls)

    requests: list[object] = []
    orig_get = fake_cls.get_most_actives

    def _capturing_get(self: object, request: object) -> object:
        requests.append(request)
        return orig_get(self, request)

    fake_cls.get_most_actives = _capturing_get  # type: ignore[method-assign]

    await list_most_active_symbols(api_key="k", secret_key="s", top=400)

    assert len(requests) == 2, "one call per ranking (volume, trades)"
    assert all(r.top == 100 for r in requests)


# ── _opt_bool tri-state ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shortable_and_easy_to_borrow_stay_none_when_unreported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` must survive as ``None``, not collapse to False: the risk
    engine's shortable_check treats "unknown" and "confirmed not
    shortable" as different, separately-audited facts."""
    row = _row(shortable=None, easy_to_borrow=None)
    monkeypatch.setattr(
        alpaca_trading_client, "TradingClient", _fake_client_class(get_asset_result=row)
    )

    info = await lookup_asset("AAPL", api_key="k", secret_key="s")

    assert info is not None
    assert info.shortable is None
    assert info.easy_to_borrow is None


@pytest.mark.asyncio
async def test_shortable_and_easy_to_borrow_map_real_booleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(shortable=False, easy_to_borrow=True)
    monkeypatch.setattr(
        alpaca_trading_client, "TradingClient", _fake_client_class(get_asset_result=row)
    )

    info = await lookup_asset("AAPL", api_key="k", secret_key="s")

    assert info is not None
    assert info.shortable is False
    assert info.easy_to_borrow is True
