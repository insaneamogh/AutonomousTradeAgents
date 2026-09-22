"""Black-Scholes: checked against textbook values and its own identities."""

from __future__ import annotations

import math

import pytest

from engine.options.pricing import delta, implied_vol, price, strike_for_delta, theta_per_day


def test_textbook_call_and_put() -> None:
    """Hull's standard example: S=100, K=100, T=1, sigma=20%, r=5%."""
    assert price(100, 100, 1.0, 0.20, kind="call", rate=0.05) == pytest.approx(10.4506, abs=1e-3)
    assert price(100, 100, 1.0, 0.20, kind="put", rate=0.05) == pytest.approx(5.5735, abs=1e-3)


def test_put_call_parity() -> None:
    s, k, t, v, r = 187.0, 190.0, 30 / 365, 0.42, 0.04
    c = price(s, k, t, v, kind="call", rate=r)
    p = price(s, k, t, v, kind="put", rate=r)
    assert c - p == pytest.approx(s - k * math.exp(-r * t), abs=1e-9)


def test_expiry_and_zero_vol_return_intrinsic() -> None:
    assert price(110, 100, 0.0, 0.3, kind="call") == 10.0
    assert price(90, 100, 0.0, 0.3, kind="call") == 0.0
    assert price(90, 100, 0.5, 0.0, kind="put") == 10.0


@pytest.mark.parametrize("kind", ["call", "put"])
@pytest.mark.parametrize("target", [0.25, 0.45, 0.65])
def test_strike_for_delta_round_trips(kind: str, target: float) -> None:
    t, v = 30 / 365, 0.35
    k = strike_for_delta(100.0, t, v, kind=kind, target_abs_delta=target)  # type: ignore[arg-type]
    assert abs(delta(100.0, k, t, v, kind=kind)) == pytest.approx(target, abs=1e-9)  # type: ignore[arg-type]


def test_a_long_option_loses_value_to_time() -> None:
    assert theta_per_day(100, 100, 20 / 365, 0.30, kind="call") < 0
    assert theta_per_day(100, 100, 20 / 365, 0.30, kind="put") < 0


def test_implied_vol_inverts_price_and_refuses_the_impossible() -> None:
    p = price(100, 105, 45 / 365, 0.27, kind="call")
    assert implied_vol(p, 100, 105, 45 / 365, kind="call") == pytest.approx(0.27, abs=1e-4)
    assert implied_vol(0.01, 100, 50, 45 / 365, kind="call") is None  # below intrinsic
