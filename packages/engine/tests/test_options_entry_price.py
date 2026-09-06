"""Entry limit pricing for long options.

Every options entry used to be submitted at the full ask. Alpaca marks a
long option near the bid, so the position opened already down the whole
spread and the -40% premium stop measured from that impaired mark. On the
real 2026-09-04 book entry spreads ran 0.1%-5.9%.
"""

from __future__ import annotations

import pytest

from engine.options import entry_limit_price


def test_pays_mid_plus_a_tick_not_the_ask() -> None:
    # The real GILD261016C00155000 entry: bid 3.67 / ask 3.82.
    assert entry_limit_price(3.67, 3.82) == 3.75


def test_never_pays_more_than_the_ask() -> None:
    """Improving on the mid must never become paying MORE than the old
    behaviour did. On a one-tick-wide market mid+tick would exceed the
    ask, so it clamps."""
    assert entry_limit_price(10.72, 10.73) == 10.73
    assert entry_limit_price(0.48, 0.49) == 0.49


def test_always_strictly_above_the_bid_so_the_order_stays_marketable() -> None:
    for bid, ask in [(3.67, 3.82), (1.21, 1.25), (2.45, 2.55), (0.90, 1.10)]:
        px = entry_limit_price(bid, ask)
        assert px is not None
        assert bid < px <= ask, f"{bid}/{ask} -> {px}"


@pytest.mark.parametrize("bid", [None, 0.0, -1.0])
def test_falls_back_to_the_ask_without_a_usable_bid(bid: float | None) -> None:
    """No credible bid means no mid worth improving on — fall back to the
    previous, safe behaviour rather than inventing a price."""
    assert entry_limit_price(bid, 2.50) == 2.50


def test_falls_back_to_the_ask_on_a_crossed_quote() -> None:
    assert entry_limit_price(2.60, 2.50) == 2.50


@pytest.mark.parametrize("ask", [None, 0.0, -1.0])
def test_unpriceable_contract_returns_none(ask: float | None) -> None:
    """None, never 0.0 — a caller must be able to tell "no price" apart
    from "free", and the broker layer builds a LimitOrderRequest straight
    off this value with no None-guard."""
    assert entry_limit_price(1.00, ask) is None
