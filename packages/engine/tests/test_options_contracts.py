"""``fetch_option_candidates`` tests — the orchestration layer between the
broker's two Alpaca calls and ``engine.options.selection.select_contract``.

Monkeypatches ``broker.alpaca.list_option_chain_quotes``/
``list_option_contracts`` directly (both are LAZY-imported inside
``fetch_option_candidates``'s own body, so patching the module attributes
works cleanly — same convention ``packages/broker/tests`` already uses for
patching ``TradingClient``/``OptionHistoricalDataClient``). Real
``alpaca.trading.models.OptionContract`` instances (via ``model_construct``)
stand in for the open-interest side of the merge — the actual SDK shape for
that call is unchanged and already covered by its own (pre-existing) code
path; what's under test here is the MERGE logic, not that SDK's shape.

Same ``websockets.legacy`` DeprecationWarning guard as the broker package's
own test files, defensively — this repo's combined suite may import
``broker.alpaca`` for the first time from either package depending on run
order.
"""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

import pytest

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    import broker.alpaca as broker_alpaca

from alpaca.trading.models import OptionContract

from engine.options.contracts import fetch_option_candidates
from engine.options.selection import ContractQuote
from engine.risk.types import RiskCaps

_NOW = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)


def _chain_quote(
    *,
    occ_symbol: str = "AAPL261016C00250000",
    underlying_symbol: str = "AAPL",
    contract_type: str = "call",
    strike: float = 250.0,
    expiry: date = date(2026, 10, 16),
    bid: float | None = 3.10,
    ask: float | None = 3.30,
    delta: float | None = 0.50,
    implied_volatility: float | None = 0.28,
    last_trade_size: float | None = 5.0,
) -> broker_alpaca.ChainQuote:
    return broker_alpaca.ChainQuote(
        occ_symbol=occ_symbol,
        underlying_symbol=underlying_symbol,
        contract_type=contract_type,
        strike=strike,
        expiry=expiry,
        bid=bid,
        ask=ask,
        delta=delta,
        implied_volatility=implied_volatility,
        last_trade_size=last_trade_size,
    )


def _option_contract(*, symbol: str, open_interest: str | None) -> OptionContract:
    return OptionContract.model_construct(symbol=symbol, open_interest=open_interest)


def _fake_chain_fn(
    *,
    result: list[broker_alpaca.ChainQuote] | None = None,
    raises: BaseException | None = None,
    received: list[dict[str, object]] | None = None,
) -> Callable[..., Awaitable[list[broker_alpaca.ChainQuote]]]:
    async def _fake(
        underlying_symbol: str,
        *,
        api_key: str,
        secret_key: str,
        feed: object = None,
        expiration_date_gte: date | None = None,
        expiration_date_lte: date | None = None,
        contract_type: object = None,
    ) -> list[broker_alpaca.ChainQuote]:
        if received is not None:
            received.append(
                {
                    "underlying_symbol": underlying_symbol,
                    "expiration_date_gte": expiration_date_gte,
                    "expiration_date_lte": expiration_date_lte,
                }
            )
        if raises is not None:
            raise raises
        return result if result is not None else []

    return _fake


def _fake_contracts_fn(
    *,
    result: list[OptionContract] | None = None,
    received: list[dict[str, object]] | None = None,
) -> Callable[..., Awaitable[list[OptionContract]]]:
    async def _fake(
        underlying_symbols: list[str],
        *,
        api_key: str,
        secret_key: str,
        expiration_date_gte: date | None = None,
        expiration_date_lte: date | None = None,
        contract_type: object = None,
    ) -> list[OptionContract]:
        if received is not None:
            received.append(
                {
                    "underlying_symbols": underlying_symbols,
                    "expiration_date_gte": expiration_date_gte,
                    "expiration_date_lte": expiration_date_lte,
                }
            )
        return result if result is not None else []

    return _fake


# ─────────────────────────────────────────────────────────────────────
# Open-interest merge — the reason this function exists, not optional
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_interest_merges_by_occ_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    occ = "AAPL261016C00250000"
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_chain_quotes",
        _fake_chain_fn(result=[_chain_quote(occ_symbol=occ)]),
    )
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_contracts",
        _fake_contracts_fn(result=[_option_contract(symbol=occ, open_interest="450")]),
    )

    candidates = await fetch_option_candidates("AAPL", api_key="k", secret_key="s", now=_NOW)

    assert candidates == (
        ContractQuote(
            occ_symbol=occ,
            contract_type="call",
            strike=250.0,
            expiry=date(2026, 10, 16),
            bid=3.10,
            ask=3.30,
            open_interest=450,
            volume=5,
            delta=0.50,
            implied_volatility=0.28,
        ),
    )


@pytest.mark.asyncio
async def test_chain_symbol_with_no_metadata_match_has_none_open_interest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge miss (the metadata call returns nothing for this OCC symbol,
    or fails and returns []) must fail closed to open_interest=None — never
    default to a value that would make an unverifiable contract look
    liquid."""
    occ = "AAPL261016C00250000"
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_chain_quotes",
        _fake_chain_fn(result=[_chain_quote(occ_symbol=occ)]),
    )
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_contracts",
        _fake_contracts_fn(
            result=[_option_contract(symbol="SOME_OTHER_SYMBOL", open_interest="999")]
        ),
    )

    candidates = await fetch_option_candidates("AAPL", api_key="k", secret_key="s", now=_NOW)

    assert len(candidates) == 1
    assert candidates[0].open_interest is None


@pytest.mark.asyncio
async def test_metadata_open_interest_none_or_unparseable_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occ = "AAPL261016C00250000"
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_chain_quotes",
        _fake_chain_fn(result=[_chain_quote(occ_symbol=occ)]),
    )
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_contracts",
        _fake_contracts_fn(result=[_option_contract(symbol=occ, open_interest="not-a-number")]),
    )

    candidates = await fetch_option_candidates("AAPL", api_key="k", secret_key="s", now=_NOW)

    assert candidates[0].open_interest is None


# ─────────────────────────────────────────────────────────────────────
# Volume proxy, empty chain
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_volume_is_last_trade_size_not_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occ = "AAPL261016C00250000"
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_chain_quotes",
        _fake_chain_fn(result=[_chain_quote(occ_symbol=occ, last_trade_size=None)]),
    )
    monkeypatch.setattr(broker_alpaca, "list_option_contracts", _fake_contracts_fn(result=[]))

    candidates = await fetch_option_candidates("AAPL", api_key="k", secret_key="s", now=_NOW)

    assert candidates[0].volume is None


@pytest.mark.asyncio
async def test_empty_chain_returns_empty_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_alpaca, "list_option_chain_quotes", _fake_chain_fn(result=[]))
    monkeypatch.setattr(broker_alpaca, "list_option_contracts", _fake_contracts_fn(result=[]))

    candidates = await fetch_option_candidates("AAPL", api_key="k", secret_key="s", now=_NOW)

    assert candidates == ()


# ─────────────────────────────────────────────────────────────────────
# DTE windowing — both calls get the SAME RiskCaps-derived bound
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dte_window_derived_from_riskcaps_reaches_both_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain_received: list[dict[str, object]] = []
    contracts_received: list[dict[str, object]] = []
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_chain_quotes",
        _fake_chain_fn(result=[], received=chain_received),
    )
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_contracts",
        _fake_contracts_fn(result=[], received=contracts_received),
    )
    caps = RiskCaps(options_min_dte=5, options_max_dte=50)

    await fetch_option_candidates("AAPL", api_key="k", secret_key="s", now=_NOW, caps=caps)

    expected_gte = date(2026, 9, 2)  # 2026-08-28 + 5 days
    expected_lte = date(2026, 10, 17)  # 2026-08-28 + 50 days
    assert chain_received[0]["expiration_date_gte"] == expected_gte
    assert chain_received[0]["expiration_date_lte"] == expected_lte
    assert contracts_received[0]["expiration_date_gte"] == expected_gte
    assert contracts_received[0]["expiration_date_lte"] == expected_lte
    assert contracts_received[0]["underlying_symbols"] == ["AAPL"]


# ─────────────────────────────────────────────────────────────────────
# Exceptions propagate — this layer does not swallow them
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chain_call_exception_propagates_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike ``list_option_chain_quotes`` itself (which catches a
    broker-side failure and returns ``[]``), this orchestration layer must
    NOT add a second layer of swallowing — the caller
    (``trading_agents.nodes.drafter._fetch_option_candidates``) is what
    degrades this to a HOLD, one level up."""
    monkeypatch.setattr(
        broker_alpaca,
        "list_option_chain_quotes",
        _fake_chain_fn(raises=RuntimeError("boom")),
    )
    monkeypatch.setattr(broker_alpaca, "list_option_contracts", _fake_contracts_fn(result=[]))

    with pytest.raises(RuntimeError, match="boom"):
        await fetch_option_candidates("AAPL", api_key="k", secret_key="s", now=_NOW)
