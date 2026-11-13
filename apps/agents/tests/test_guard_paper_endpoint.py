"""``_is_paper_and_safe`` must also require a paper Alpaca endpoint.

The agent tools place orders via ``AlpacaBroker.from_env()``, which selects
LIVE when ALPACA_BASE_URL is set and lacks "paper". The guard must agree.
"""

from __future__ import annotations

import pytest

from trading_agents.options.tools.guard import _is_paper_and_safe


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TRADING_MODE", "LIVE_TRADING_ENABLED", "ALPACA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_paper_when_base_url_unset():
    assert _is_paper_and_safe() is True


def test_paper_when_base_url_is_paper_endpoint(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    assert _is_paper_and_safe() is True


def test_not_safe_when_base_url_is_live_endpoint(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    assert _is_paper_and_safe() is False


def test_not_safe_when_trading_mode_live(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    assert _is_paper_and_safe() is False


def test_not_safe_when_live_trading_enabled(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    assert _is_paper_and_safe() is False
