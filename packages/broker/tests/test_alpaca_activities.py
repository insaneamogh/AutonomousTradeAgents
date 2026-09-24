"""AlpacaBroker.list_option_lifecycle_activities: parsing and paging.

Rows are shaped like Alpaca's documented option-lifecycle examples
(docs.alpaca.markets, "Options Trading": OPEXC/OPTRD for an exercise,
OPASN/OPTRD for an assignment, OPEXP for an expiry). qty, price and
net_amount arrive as decimal STRINGS, per the account-activities reference.
"""

from __future__ import annotations

import warnings
from datetime import date

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    from broker.alpaca import (
        _ACTIVITY_PAGE_SIZE,
        AlpacaBroker,
        _activity_from_alpaca,
    )


class _FakeClient:
    def __init__(self, pages: list[list[dict]]) -> None:
        self._pages = list(pages)
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, data: dict | None = None) -> list[dict]:
        self.calls.append((path, dict(data or {})))
        return self._pages.pop(0) if self._pages else []


def _broker(client: _FakeClient) -> AlpacaBroker:
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.is_paper = True
    b._client = client
    return b


_EXERCISE = [
    {"id": "20240301000000000::a", "activity_type": "OPEXC", "date": "2024-03-01",
     "symbol": "QS240301C00006500", "qty": "-1", "net_amount": "0"},
    {"id": "20240301000000000::b", "activity_type": "OPTRD", "date": "2024-03-01",
     "symbol": "QS", "qty": "100", "price": "6.5", "net_amount": "-650"},
]


def test_parses_documented_exercise_pair() -> None:
    parsed = [_activity_from_alpaca(r) for r in _EXERCISE]
    assert parsed[0] is not None and parsed[1] is not None
    assert parsed[0].activity_type == "OPEXC"
    assert parsed[0].symbol == "QS240301C00006500"
    assert parsed[0].qty == -1.0
    assert parsed[0].day == date(2024, 3, 1)
    assert parsed[1].symbol == "QS"
    assert parsed[1].qty == 100.0
    assert parsed[1].price == 6.5
    assert parsed[1].net_amount == -650.0


def test_malformed_rows_are_dropped_not_raised() -> None:
    assert _activity_from_alpaca({"activity_type": "OPEXP", "date": "2024-03-01"}) is None
    assert _activity_from_alpaca({"activity_type": "OPEXP", "symbol": "X", "date": "bad"}) is None
    assert _activity_from_alpaca("not a dict") is None


async def test_queries_lifecycle_types_after_since() -> None:
    client = _FakeClient([_EXERCISE])
    out = await _broker(client).list_option_lifecycle_activities(since=date(2024, 2, 20))

    assert [a.activity_type for a in out] == ["OPEXC", "OPTRD"]
    path, params = client.calls[0]
    assert path == "/account/activities"
    assert params["after"] == "2024-02-20"
    assert set(params["activity_types"].split(",")) == {"OPEXP", "OPEXC", "OPASN", "OPTRD"}
    assert len(client.calls) == 1  # a short page ends paging


async def test_pages_with_the_last_id_until_a_short_page() -> None:
    full = [
        {"id": f"id-{i}", "activity_type": "OPEXP", "date": "2024-03-01",
         "symbol": f"QS240301C{i:08d}", "qty": "-1", "net_amount": "0"}
        for i in range(_ACTIVITY_PAGE_SIZE)
    ]
    tail = [{"id": "id-last", "activity_type": "OPEXP", "date": "2024-03-01",
             "symbol": "QS240301P00000500", "qty": "-1", "net_amount": "0"}]
    client = _FakeClient([full, tail])

    out = await _broker(client).list_option_lifecycle_activities(since=date(2024, 2, 1))

    assert len(out) == _ACTIVITY_PAGE_SIZE + 1
    assert len(client.calls) == 2
    assert client.calls[1][1]["page_token"] == f"id-{_ACTIVITY_PAGE_SIZE - 1}"


def test_a_bracket_parents_legs_are_mapped_with_their_order_type() -> None:
    """order_sync closes an equity decision from its bracket's filled leg;
    the legs exist only on the parent order Alpaca returns."""
    from types import SimpleNamespace

    from alpaca.trading.enums import OrderSide, OrderStatus, OrderType

    def _raw(oid: str, kind: OrderType, status: OrderStatus, avg: str | None, legs=None):
        return SimpleNamespace(
            id=oid, symbol="AAPL", side=OrderSide.SELL, qty="10", filled_qty="10" if avg else "0",
            filled_avg_price=avg, submitted_at=None, filled_at=None, status=status,
            client_order_id=None, order_type=kind, legs=legs,
        )

    parent = _raw("p1", OrderType.MARKET, OrderStatus.FILLED, "200", legs=[
        _raw("tp", OrderType.LIMIT, OrderStatus.CANCELED, None),
        _raw("sl", OrderType.STOP, OrderStatus.FILLED, "189.5"),
    ])
    order = _broker(_FakeClient([]))._order_from_alpaca(parent)
    assert [(leg.broker_order_id, leg.raw["order_type"]) for leg in order.legs] == [
        ("tp", "limit"), ("sl", "stop")]
    assert order.legs[1].avg_fill_price == 189.5
