"""`list_option_contracts` must paginate.

This endpoint defaults to 100 rows and does NOT auto-paginate (unlike the
market-data client, whose `_get_marketdata` loops on next_page_token).
It is the ONLY source of `open_interest`, and
`engine.options.selection._passes_liquidity` hard-fails on
`open_interest is None` by design — so an unpaged fetch left 98% of every
chain with no OI, every one failed the liquidity stage, and the funnel
emptied to zero on EVERY symbol including SPY. Options could never trade.

Measured live 2026-08-31 (SPY, 7-60 DTE): 100 rows unpaged vs 4674 paged,
2024 of which carry open_interest >= 100.
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace

import pytest

with warnings.catch_warnings():
    # Same suppression as test_alpaca.py — see its module docstring for why
    # the first import of broker.alpaca in this package needs it.
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    from broker import alpaca as mod

from alpaca.trading import client as alpaca_trading_client


class _FakeClient:
    """Returns 3 pages, then stops — mirrors Alpaca's token protocol."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    def get_option_contracts(self, request):
        self.requests.append(request)
        n = len(self.requests)
        if n < 3:
            return SimpleNamespace(
                option_contracts=[SimpleNamespace(symbol=f"P{n}C{i}") for i in range(2)],
                next_page_token=f"tok{n}",
            )
        return SimpleNamespace(
            option_contracts=[SimpleNamespace(symbol="LAST")], next_page_token=None
        )


@pytest.fixture
def _fake(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr(alpaca_trading_client, "TradingClient", lambda **kw: client)
    return client


async def test_follows_next_page_token_to_exhaustion(_fake: _FakeClient) -> None:
    out = await mod.list_option_contracts(["SPY"], api_key="k", secret_key="s")
    assert len(out) == 5  # 2 + 2 + 1
    assert len(_fake.requests) == 3


async def test_forwards_the_page_token_on_each_subsequent_call(_fake: _FakeClient) -> None:
    await mod.list_option_contracts(["SPY"], api_key="k", secret_key="s")
    tokens = [getattr(r, "page_token", "MISSING") for r in _fake.requests]
    assert tokens == [None, "tok1", "tok2"]


async def test_requests_a_large_page_not_the_100_default(_fake: _FakeClient) -> None:
    """The whole bug: the default page size silently truncated the chain."""
    await mod.list_option_contracts(["SPY"], api_key="k", secret_key="s")
    assert getattr(_fake.requests[0], "limit", None) == mod._CONTRACT_PAGE_LIMIT
    assert mod._CONTRACT_PAGE_LIMIT > 100


async def test_page_loop_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed token must not spin forever inside a council pass."""

    class _Endless:
        def get_option_contracts(self, request):
            return SimpleNamespace(
                option_contracts=[SimpleNamespace(symbol="X")], next_page_token="always"
            )

    monkeypatch.setattr(alpaca_trading_client, "TradingClient", lambda **kw: _Endless())
    out = await mod.list_option_contracts(["SPY"], api_key="k", secret_key="s")
    assert len(out) == mod._MAX_CONTRACT_PAGES


async def test_partial_data_is_returned_when_a_later_page_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial beats none: an OI-less contract merely fails the liquidity
    gate, which is the safe direction."""

    class _FailsOnSecond:
        def __init__(self) -> None:
            self.n = 0

        def get_option_contracts(self, request):
            self.n += 1
            if self.n == 1:
                return SimpleNamespace(
                    option_contracts=[SimpleNamespace(symbol="A")], next_page_token="t"
                )
            raise RuntimeError("boom")

    monkeypatch.setattr(alpaca_trading_client, "TradingClient", lambda **kw: _FailsOnSecond())
    out = await mod.list_option_contracts(["SPY"], api_key="k", secret_key="s")
    assert [c.symbol for c in out] == ["A"]
