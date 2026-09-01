"""universe_refresh.screen_universe — pure filtering logic, no DB/network.

Mocks broker.alpaca's two functions at the point universe_refresh imports
them (it does so lazily, inside screen_universe's body, so the patch
target is broker.alpaca itself — see test_alpaca.py's own docstring for
why a lazy `from X import Y` must be patched on X, not on the caller's
namespace).
"""

from __future__ import annotations

import warnings

import pytest

# Same pre-existing alpaca-py/websockets version mismatch test_alpaca.py's
# own docstring documents: importing broker.alpaca for the first time in a
# session pulls in alpaca.trading.stream's deprecated `websockets.legacy`
# import, which this repo's `filterwarnings = ["error"]` would otherwise
# turn into a collection-time crash on whichever test happens to trigger
# the import first.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    import broker.alpaca as broker_alpaca

from trading_agents.jobs.universe_refresh import screen_universe


class _Asset:
    def __init__(
        self,
        symbol: str,
        *,
        tradable: bool = True,
        fractionable: bool = True,
        has_options: bool = False,
    ) -> None:
        self.symbol = symbol
        self.tradable = tradable
        self.fractionable = fractionable
        self.has_options = has_options


async def _fake_list_most_active_symbols(*, api_key: str, secret_key: str, top: int) -> list[str]:
    return ["NVDA", "PENNY", "AAPL", "HALTD", "TSLA", "ILLQ"][:top]


def _fake_assets() -> list[_Asset]:
    return [
        _Asset("NVDA", has_options=True),
        _Asset("PENNY", fractionable=False),  # low-quality, activity-only noise
        _Asset("AAPL", has_options=True),
        _Asset("HALTD", tradable=False),  # delisted/halted since the activity snapshot
        _Asset("TSLA", has_options=False),  # tradable, but not options-eligible
        # ILLQ deliberately absent — a symbol Alpaca's screener returned
        # that isn't in the tradable-assets response at all.
    ]


async def _fake_list_tradable_assets(*, api_key: str, secret_key: str) -> list[_Asset]:
    return _fake_assets()


@pytest.fixture(autouse=True)
def _patch_broker_alpaca(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_alpaca, "list_most_active_symbols", _fake_list_most_active_symbols)
    monkeypatch.setattr(broker_alpaca, "list_tradable_assets", _fake_list_tradable_assets)


async def test_drops_non_tradable_and_non_fractionable_noise() -> None:
    equity, _options = await screen_universe(api_key="k", secret_key="s")
    assert "PENNY" not in equity, "not fractionable -- quality filter must drop it"
    assert "HALTD" not in equity, "not tradable -- must drop it"
    assert "ILLQ" not in equity, "absent from the tradable-assets response -- must drop it"


async def test_keeps_real_tradable_fractionable_names_in_activity_order() -> None:
    equity, _options = await screen_universe(api_key="k", secret_key="s")
    assert equity == ["NVDA", "AAPL", "TSLA"], (
        "must preserve the activity-ranked order and include every tradable+fractionable survivor"
    )


async def test_options_list_is_the_has_options_subset_only() -> None:
    _equity, options = await screen_universe(api_key="k", secret_key="s")
    assert options == ["NVDA", "AAPL"], "TSLA is tradable but not options-eligible"


async def test_max_equity_and_max_options_caps_are_independent() -> None:
    equity, options = await screen_universe(
        api_key="k", secret_key="s", max_equity=1, max_options=1
    )
    assert equity == ["NVDA"]
    assert options == ["NVDA"]


async def test_empty_activity_pool_yields_empty_candidates() -> None:
    equity, options = await screen_universe(api_key="k", secret_key="s", activity_pool=0)
    assert equity == []
    assert options == []
