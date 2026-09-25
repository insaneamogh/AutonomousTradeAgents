"""Zerodha round-trip costs, worked by hand from zerodha.com/charges
(read 2026-09-25). Each case lists the arithmetic it pins."""

from __future__ import annotations

import pytest

from engine.backtester.costs_india import round_trip, round_trip_pct


@pytest.mark.parametrize(
    ("segment", "buy", "sell", "expected"),
    [
        # STT 0.1% x 2L = 200.00; NSE 0.00307% x 2L = 6.14; SEBI 0.20; stamp
        # 0.015% x 1L = 15.00; GST 18% x (0 + 0.20 + 6.14) = 1.14; DP 15.34.
        ("equity_delivery", 100_000.0, 100_000.0,
         dict(brokerage=0.0, stt=200.0, exchange=6.14, sebi=0.2, stamp=15.0, gst=1.14,
              dp=15.34, total=237.82)),
        # Brokerage min(0.03% x 1L, 20) x 2 = 40; STT 0.025% x 1L = 25; NSE
        # 6.14; SEBI 0.20; stamp 0.003% x 1L = 3.00; GST 18% x 46.34 = 8.34.
        ("equity_intraday", 100_000.0, 100_000.0,
         dict(brokerage=40.0, stt=25.0, exchange=6.14, sebi=0.2, stamp=3.0, gst=8.34,
              dp=0.0, total=82.68)),
        # Rs 20 x 2 = 40; STT 0.15% x 12,000 = 18.00; NSE 0.03553% x 22,000 =
        # 7.82; SEBI 0.02; stamp 0.003% x 10,000 = 0.30; GST 18% x 47.84 = 8.61.
        ("options", 10_000.0, 12_000.0,
         dict(brokerage=40.0, stt=18.0, exchange=7.82, sebi=0.02, stamp=0.3, gst=8.61,
              dp=0.0, total=74.75)),
    ],
)
def test_round_trip(segment, buy, sell, expected) -> None:
    c = round_trip(segment, buy_value=buy, sell_value=sell)
    got = dict(brokerage=c.brokerage, stt=c.stt, exchange=c.exchange, sebi=c.sebi,
               stamp=c.stamp, gst=c.gst, dp=c.dp, total=c.total)
    assert got == expected


def test_a_long_option_left_to_exercise_pays_stt_on_its_intrinsic_value() -> None:
    """The trap the expiry sweep exists to avoid: no sale, one order, and
    0.15% STT on the whole intrinsic value (15,000 -> 22.50)."""
    c = round_trip("options", buy_value=10_000.0, sell_value=0.0,
                   exercised_intrinsic_value=15_000.0)
    assert (c.brokerage, c.stt) == (20.0, 22.5)


def test_a_delivery_round_trip_costs_about_a_quarter_percent() -> None:
    assert round_trip_pct("equity_delivery", buy_value=100_000.0, sell_value=100_000.0) == 0.2378
