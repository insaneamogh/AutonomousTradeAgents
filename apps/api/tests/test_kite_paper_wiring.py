"""ZERODHA_PAPER swaps a Zerodha connection onto the persisted paper book.

The book itself is proven end to end in tests/e2e/test_e2e_zerodha.py;
this pins the switch, because it decides whether an order is real money.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.broker.broker_store import BrokerConnectionRecord


def _conn() -> BrokerConnectionRecord:
    return BrokerConnectionRecord(
        id="c1", user_id="00000000-0000-0000-0000-000000000001", broker="zerodha",
        is_paper=False, account_number=None, encrypted_access_token="x",
        encrypted_refresh_token=None, access_token_expires_at=datetime.now(UTC),
        refresh_token_expires_at=None,
    )


@pytest.mark.parametrize("flag", ["", "0", "1"])
def test_zerodha_paper_decides_whether_an_order_can_reach_kite(
    monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    from app.services.broker.broker_use import _paper_zerodha_if_enabled
    from app.services.orders.kite_paper import KitePaperBroker

    monkeypatch.setenv("ZERODHA_PAPER", flag)
    real, conn = object(), _conn()
    client, used = _paper_zerodha_if_enabled(real, conn, conn.user_id)
    if flag == "1":
        assert isinstance(client, KitePaperBroker) and client._md is real
        assert used.is_paper is True and conn.is_paper is False, "a copy, not a mutation"
    else:
        assert client is real and used is conn and used.is_paper is False
