"""``list_option_chain_quotes`` unit tests — the function that replaces the
options-chain integration bug this task exists to fix.

Fixtures are built from the REAL alpaca-py pydantic models via
``model_construct`` (skips validation, still a genuine instance of the real
class) rather than ``types.SimpleNamespace`` — deliberately, unlike
``test_alpaca.py``'s own convention for ``lookup_asset``/
``list_tradable_assets``. Those functions read every field via
``getattr(..., default)``, so a loose fixture is fine; this function reads
REAL attribute access (see its own docstring in ``broker/alpaca.py`` for
why), so only a real model instance actually proves the field names it
assumes still exist on the installed SDK.

Same ``websockets.legacy`` DeprecationWarning guard as ``test_alpaca.py`` /
``test_alpaca_options.py`` around the first import of ``broker.alpaca`` in
this test — see either of those modules' docstrings for the full story.
"""

from __future__ import annotations

import warnings
from datetime import UTC, date, datetime

import pytest

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    import alpaca.data.historical.option as alpaca_option_data

    from broker.alpaca import ChainQuote, list_option_chain_quotes

from alpaca.data.enums import OptionsFeed
from alpaca.data.models.quotes import Quote
from alpaca.data.models.snapshots import OptionsGreeks, OptionsSnapshot
from alpaca.data.models.trades import Trade
from alpaca.data.requests import OptionChainRequest


def _fake_option_data_client_class(
    *,
    get_option_chain_result: dict[str, OptionsSnapshot] | None = None,
    get_option_chain_raises: BaseException | None = None,
    received_requests: list[OptionChainRequest] | None = None,
) -> type[object]:
    """Stand-in ``OptionHistoricalDataClient`` class — mirrors
    ``test_alpaca.py``'s ``_fake_client_class`` precedent exactly: a class,
    not an instance, since ``list_option_chain_quotes`` constructs a fresh
    client per call via a LAZY import inside its own function body."""
    result = get_option_chain_result if get_option_chain_result is not None else {}

    class FakeOptionHistoricalDataClient:
        def __init__(self, **kwargs: object) -> None:
            self.init_kwargs = kwargs

        def get_option_chain(self, request: OptionChainRequest) -> dict[str, OptionsSnapshot]:
            if received_requests is not None:
                received_requests.append(request)
            if get_option_chain_raises is not None:
                raise get_option_chain_raises
            return result

    return FakeOptionHistoricalDataClient


def _quote(*, bid_price: float, ask_price: float) -> Quote:
    return Quote.model_construct(
        symbol="X", timestamp=datetime.now(UTC), bid_price=bid_price, ask_price=ask_price
    )


def _trade(*, size: float) -> Trade:
    return Trade.model_construct(symbol="X", timestamp=datetime.now(UTC), price=0.0, size=size)


def _greeks(*, delta: float) -> OptionsGreeks:
    return OptionsGreeks.model_construct(delta=delta, gamma=0.0, rho=0.0, theta=0.0, vega=0.0)


def _snapshot(
    symbol: str,
    *,
    quote: Quote | None = None,
    trade: Trade | None = None,
    greeks: OptionsGreeks | None = None,
    implied_volatility: float | None = None,
) -> OptionsSnapshot:
    return OptionsSnapshot.model_construct(
        symbol=symbol,
        latest_quote=quote,
        latest_trade=trade,
        greeks=greeks,
        implied_volatility=implied_volatility,
    )


# ─────────────────────────────────────────────────────────────────────
# Real-fixture field mapping — the OPTIONS_PLAN.md §0 live-verified example
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maps_the_live_verified_example_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """AAPL260828P00305000, δ -0.2790, IV 0.2644 — the exact contract
    docs/OPTIONS_PLAN.md §0 measured live against a real paper account."""
    occ = "AAPL260828P00305000"
    snapshot = _snapshot(
        occ,
        quote=_quote(bid_price=4.85, ask_price=5.05),
        trade=_trade(size=3.0),
        greeks=_greeks(delta=-0.2790),
        implied_volatility=0.2644,
    )
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(get_option_chain_result={occ: snapshot}),
    )

    quotes = await list_option_chain_quotes("AAPL", api_key="k", secret_key="s")

    assert quotes == [
        ChainQuote(
            occ_symbol=occ,
            underlying_symbol="AAPL",
            contract_type="put",
            strike=305.0,
            expiry=date(2026, 8, 28),
            bid=4.85,
            ask=5.05,
            delta=-0.2790,
            implied_volatility=0.2644,
            last_trade_size=3.0,
        )
    ]


@pytest.mark.asyncio
async def test_greeks_none_on_deep_itm_leaves_delta_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/OPTIONS_PLAN.md §0: greeks are absent (null) on deep-ITM
    strikes and some 0DTE contracts — the plan must tolerate this, not
    crash on it."""
    occ = "AAPL260828C00100000"
    snapshot = _snapshot(
        occ,
        quote=_quote(bid_price=150.0, ask_price=151.0),
        trade=_trade(size=1.0),
        greeks=None,
        implied_volatility=None,
    )
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(get_option_chain_result={occ: snapshot}),
    )

    quotes = await list_option_chain_quotes("AAPL", api_key="k", secret_key="s")

    assert len(quotes) == 1
    assert quotes[0].delta is None
    assert quotes[0].implied_volatility is None


@pytest.mark.asyncio
async def test_latest_quote_none_leaves_bid_ask_none(monkeypatch: pytest.MonkeyPatch) -> None:
    occ = "AAPL260828C00100000"
    snapshot = _snapshot(occ, quote=None, trade=_trade(size=1.0), greeks=_greeks(delta=0.5))
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(get_option_chain_result={occ: snapshot}),
    )

    quotes = await list_option_chain_quotes("AAPL", api_key="k", secret_key="s")

    assert quotes[0].bid is None
    assert quotes[0].ask is None


@pytest.mark.asyncio
async def test_latest_trade_none_leaves_last_trade_size_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occ = "AAPL260828C00100000"
    snapshot = _snapshot(occ, quote=_quote(bid_price=1.0, ask_price=1.1), trade=None)
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(get_option_chain_result={occ: snapshot}),
    )

    quotes = await list_option_chain_quotes("AAPL", api_key="k", secret_key="s")

    assert quotes[0].last_trade_size is None


# ─────────────────────────────────────────────────────────────────────
# Robustness: unparseable key, broker failure
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unparseable_occ_symbol_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad key must not sink the whole batch — the good contract in
    the same response is still returned."""
    good_occ = "AAPL260828C00100000"
    result = {
        "not-a-real-occ-symbol": _snapshot("not-a-real-occ-symbol"),
        good_occ: _snapshot(good_occ, quote=_quote(bid_price=1.0, ask_price=1.1)),
    }
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(get_option_chain_result=result),
    )

    quotes = await list_option_chain_quotes("AAPL", api_key="k", secret_key="s")

    assert len(quotes) == 1
    assert quotes[0].occ_symbol == good_occ


@pytest.mark.asyncio
async def test_client_raises_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(get_option_chain_raises=RuntimeError("network down")),
    )

    quotes = await list_option_chain_quotes("AAPL", api_key="k", secret_key="s")

    assert quotes == []


# ─────────────────────────────────────────────────────────────────────
# Feed tier + expiry windowing reach the real request object
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_feed_is_indicative(monkeypatch: pytest.MonkeyPatch) -> None:
    """The free Basic tier every account already has — see
    docs/OPTIONS_PLAN.md §0. Must never silently request OPRA (a paid
    tier the account may not be entitled to) by default."""
    monkeypatch.delenv("ALPACA_OPTIONS_FEED", raising=False)
    received: list[OptionChainRequest] = []
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(received_requests=received),
    )

    await list_option_chain_quotes("AAPL", api_key="k", secret_key="s")

    assert received[0].feed == OptionsFeed.INDICATIVE


@pytest.mark.asyncio
async def test_env_override_selects_opra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_OPTIONS_FEED", "OPRA")  # case-insensitive
    received: list[OptionChainRequest] = []
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(received_requests=received),
    )

    await list_option_chain_quotes("AAPL", api_key="k", secret_key="s")

    assert received[0].feed == OptionsFeed.OPRA


@pytest.mark.asyncio
async def test_expiration_window_reaches_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[OptionChainRequest] = []
    monkeypatch.setattr(
        alpaca_option_data,
        "OptionHistoricalDataClient",
        _fake_option_data_client_class(received_requests=received),
    )
    gte, lte = date(2026, 9, 1), date(2026, 10, 15)

    await list_option_chain_quotes(
        "AAPL", api_key="k", secret_key="s", expiration_date_gte=gte, expiration_date_lte=lte
    )

    assert received[0].expiration_date_gte == gte
    assert received[0].expiration_date_lte == lte
    assert received[0].underlying_symbol == "AAPL"
