"""Options-specific AlpacaBroker behavior — Phase A (long calls/puts only).

Covers four things: the BUY_TO_OPEN/SELL_TO_CLOSE side + position_intent
mapping, the bracket-on-options guard, options positions surfacing
is_option/multiplier correctly, and get_options_trading_level reading the
real Alpaca account field. See ``test_alpaca.py``'s own module docstring
for why the websockets.legacy DeprecationWarning is suppressed around the
first import of ``broker.alpaca`` in this package's test suite.
"""

from __future__ import annotations

import types
import warnings

import pytest

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    from broker.alpaca import AlpacaBroker, OptionBracketNotSupportedError

from alpaca.trading.models import TradeAccount

from broker.types import OrderRequest, OrderType, Side, TimeInForce


def _broker() -> AlpacaBroker:
    """A real AlpacaBroker — TradingClient's __init__ does no network I/O,
    so this is safe to construct directly and exercise its pure request/
    response mapping methods without mocking the wire."""
    return AlpacaBroker(api_key="k", secret_key="s", paper=True)


def _alpaca_position(**overrides: object) -> types.SimpleNamespace:
    base: dict[str, object] = dict(
        symbol="AAPL",
        qty="10",
        avg_entry_price="150.00",
        market_value="1500.00",
        unrealized_pl="50.00",
        unrealized_plpc="0.0345",
        asset_class="us_equity",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ─────────────────────────────────────────────────────────────────────
# Side / position_intent mapping
# ─────────────────────────────────────────────────────────────────────


def test_buy_to_open_maps_to_buy_side_and_buy_to_open_intent() -> None:
    broker = _broker()
    request = broker._build_alpaca_request(
        OrderRequest(
            symbol="AAPL260828C00250000",
            side=Side.BUY_TO_OPEN,
            qty=1,
            order_type=OrderType.LIMIT,
            limit_price=2.50,
            time_in_force=TimeInForce.DAY,
        )
    )
    assert request.side is not None and request.side.value == "buy"
    assert request.position_intent is not None
    assert request.position_intent.value == "buy_to_open"


def test_sell_to_close_maps_to_sell_side_and_sell_to_close_intent() -> None:
    broker = _broker()
    request = broker._build_alpaca_request(
        OrderRequest(
            symbol="AAPL260828C00250000",
            side=Side.SELL_TO_CLOSE,
            qty=1,
            order_type=OrderType.LIMIT,
            limit_price=2.50,
            time_in_force=TimeInForce.DAY,
        )
    )
    assert request.side is not None and request.side.value == "sell"
    assert request.position_intent is not None
    assert request.position_intent.value == "sell_to_close"


def test_plain_equity_sides_carry_no_position_intent() -> None:
    """Equity orders must NOT suddenly grow a position_intent — the
    options-only Side values are the only ones that map to one."""
    broker = _broker()
    buy_request = broker._build_alpaca_request(
        OrderRequest(symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    )
    sell_request = broker._build_alpaca_request(
        OrderRequest(symbol="AAPL", side=Side.SELL, qty=10, order_type=OrderType.MARKET)
    )
    assert buy_request.position_intent is None
    assert sell_request.position_intent is None


# ─────────────────────────────────────────────────────────────────────
# Bracket-on-options guard
# ─────────────────────────────────────────────────────────────────────


def test_bracket_request_on_an_option_symbol_raises() -> None:
    broker = _broker()
    with pytest.raises(OptionBracketNotSupportedError):
        broker._build_alpaca_request(
            OrderRequest(
                symbol="AAPL260828C00250000",
                side=Side.BUY_TO_OPEN,
                qty=1,
                order_type=OrderType.LIMIT,
                limit_price=2.50,
                take_profit_price=3.00,
                stop_loss_price=2.00,
            )
        )


def test_bracket_request_on_an_equity_symbol_still_works() -> None:
    """Unchanged behavior pin: the guard is scoped to option symbols only
    — a plain equity bracket must keep working exactly as before."""
    broker = _broker()
    request = broker._build_alpaca_request(
        OrderRequest(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            order_type=OrderType.MARKET,
            take_profit_price=160.0,
            stop_loss_price=140.0,
        )
    )
    assert request.order_class is not None and request.order_class.value == "bracket"


# ─────────────────────────────────────────────────────────────────────
# Options position mapping
# ─────────────────────────────────────────────────────────────────────


def test_us_option_position_surfaces_is_option_and_multiplier_100() -> None:
    broker = _broker()
    raw = _alpaca_position(
        symbol="AAPL260828C00250000",
        qty="1",
        avg_entry_price="2.50",
        market_value="300.00",
        asset_class="us_option",
    )
    position = broker._position_from_alpaca(raw)
    assert position.is_option is True
    assert position.multiplier == 100


def test_us_equity_position_stays_multiplier_1_not_an_option() -> None:
    broker = _broker()
    raw = _alpaca_position(asset_class="us_equity")
    position = broker._position_from_alpaca(raw)
    assert position.is_option is False
    assert position.multiplier == 1


def test_position_with_no_asset_class_defaults_to_not_an_option() -> None:
    """A payload that omits asset_class entirely must fail closed to
    "not an option" — never silently treat unknown as us_option."""
    broker = _broker()
    raw = types.SimpleNamespace(
        symbol="AAPL",
        qty="10",
        avg_entry_price="150.00",
        market_value="1500.00",
        unrealized_pl="50.00",
        unrealized_plpc="0.0345",
    )
    position = broker._position_from_alpaca(raw)
    assert position.is_option is False
    assert position.multiplier == 1


# ─────────────────────────────────────────────────────────────────────
# get_options_trading_level — reads a real field on the real Alpaca
# account model. Previously untested anywhere in this package: an audit
# pass over the options test suite for the same blind-spot pattern the
# chain-fetch bug exhibited (see fable5findings.md's build log) found this
# one gap. Unlike that bug, the field name itself was already
# live-verified correct (docs/OPTIONS_PLAN.md §0) — this closes the
# coverage gap, it does not fix an active bug.
# ─────────────────────────────────────────────────────────────────────


class _FakeClient:
    """Stand-in for the REAL ``TradingClient`` instance ``AlpacaBroker``
    already constructed in ``__init__`` — unlike the module-level functions
    tested elsewhere in this package (``lookup_asset``, etc.), which build
    a fresh client per call and so are patchable via the CLASS on
    ``alpaca.trading.client``, ``get_options_trading_level`` calls
    ``self._client.get_account()`` on the instance the broker already
    holds. Patching the class after construction wouldn't touch that
    already-set attribute — swapping ``broker._client`` directly is the
    correct seam here."""

    def __init__(self, account: object) -> None:
        self._account = account

    def get_account(self) -> object:
        return self._account


@pytest.mark.asyncio
async def test_get_options_trading_level_reads_the_real_account_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmed via the installed alpaca-py model itself:
    TradeAccount.model_fields includes options_trading_level literally —
    this is real attribute access via getattr, reading a field that
    genuinely exists, not a guessed name."""
    broker = _broker()
    monkeypatch.setattr(
        broker, "_client", _FakeClient(TradeAccount.model_construct(options_trading_level=3))
    )

    level = await broker.get_options_trading_level()

    assert level == 3


@pytest.mark.asyncio
async def test_get_options_trading_level_none_on_an_unapproved_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field is genuinely absent/None on an account that never applied
    for options approval — this is a real account state, not an error, so
    the getattr default (not a raise) is the correct behavior here, unlike
    the chain-fetch bug's silent getattr-masks-a-real-mismatch case."""
    broker = _broker()
    monkeypatch.setattr(
        broker,
        "_client",
        _FakeClient(TradeAccount.model_construct(options_trading_level=None)),
    )

    level = await broker.get_options_trading_level()

    assert level is None
