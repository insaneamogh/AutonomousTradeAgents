"""Flatten-all: stop new entries, then close everything through the normal
risk-gated close paths. Never a bypass."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import app.services.broker.broker_store as broker_store_mod
import app.services.orders.position_manager as pm
import app.services.orders.positions_service as ps
from app.services.orders.kill_switch import KILL_SWITCH_REASON, flatten_all_now

USER = "00000000-0000-0000-0000-000000000001"


class _Store:
    def __init__(self, conns: list[Any]) -> None:
        self.conns = conns
        self.revoked: list[str] = []

    async def list_connections(self, user_id: str) -> list[Any]:
        return self.conns

    async def set_auto_approve_consent(self, connection_id: str, *, enabled: bool) -> bool:
        assert enabled is False
        self.revoked.append(connection_id)
        return True


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"managed": [], "unmanaged": []}
    store = _Store([
        SimpleNamespace(id="c1", auto_approve_consent=True),
        SimpleNamespace(id="c2", auto_approve_consent=False),
    ])
    positions = [
        SimpleNamespace(symbol="NVDA261016C00190000", managed=True, decision_id="d1"),
        SimpleNamespace(symbol="AAPL", managed=False, decision_id=None),
        SimpleNamespace(symbol="GILD", managed=True, decision_id="d2"),
    ]

    async def fake_list(user_id: str) -> list[Any]:
        return positions

    async def fake_close(*, user_id, decision_id, session_factory, reason="user_manual"):
        calls["managed"].append((decision_id, reason))
        if decision_id == "d2":
            raise RuntimeError("broker 500")
        return {"closed": True, "error": None}

    async def fake_unmanaged(*, user_id, symbol, session_factory):
        calls["unmanaged"].append(symbol)
        return {"closed": True, "error": None}

    monkeypatch.setattr(broker_store_mod, "get_broker_store", lambda: store)
    monkeypatch.setattr(ps, "list_open_positions", fake_list)
    monkeypatch.setattr(pm, "close_position_now", fake_close)
    monkeypatch.setattr(pm, "close_unmanaged_position_now", fake_unmanaged)
    calls["store"] = store
    return calls


async def test_every_position_goes_through_its_normal_close_path(wired) -> None:
    result = await flatten_all_now(user_id=USER, session_factory=None)
    assert wired["managed"] == [("d1", KILL_SWITCH_REASON), ("d2", KILL_SWITCH_REASON)]
    assert wired["unmanaged"] == ["AAPL"]
    assert len(result["positions"]) == 3


async def test_auto_approve_is_revoked_where_it_was_on(wired) -> None:
    result = await flatten_all_now(user_id=USER, session_factory=None)
    assert wired["store"].revoked == ["c1"]
    assert result["auto_approve_revoked"] == 1


async def test_one_failed_close_does_not_stop_the_rest(wired) -> None:
    result = await flatten_all_now(user_id=USER, session_factory=None)
    by = {r["symbol"]: r for r in result["positions"]}
    assert by["GILD"] == {"symbol": "GILD", "closed": False, "error": "RuntimeError"}
    assert by["AAPL"]["closed"] is True
    assert by["NVDA261016C00190000"]["closed"] is True


def test_the_reason_fits_the_close_reason_column() -> None:
    """agent_decisions.close_reason is String(20)."""
    assert len(KILL_SWITCH_REASON) <= 20
