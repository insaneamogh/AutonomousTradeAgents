"""Indian transaction costs for a Zerodha round trip (docs/PLAN_ZERODHA.md Z3).

Rates as published on zerodha.com/charges, read 2026-09-25 (the page shows
no effective date, so re-check before trusting a number here):

  segment            brokerage                 STT                         NSE txn     stamp (buy)
  equity delivery    0                         0.1% buy AND sell           0.00307%    0.015%
  equity intraday    0.03% or Rs 20, lower     0.025% sell                 0.00307%    0.003%
  index/stock opts   Rs 20 per executed order  0.15% sell (on premium);    0.03553%    0.003%
                                               0.15% of intrinsic value if       (on premium)
                                               bought and EXERCISED
  all                SEBI Rs 10 per crore; GST 18% on (brokerage + SEBI + txn)
  delivery sell      DP charge Rs 15.34 per scrip

Why it matters here: an edgeless signal loses mostly to costs (the US
option backtest: execution cost is worth ~3 points per trade), and these
are larger than US costs: a delivery round trip pays STT twice. The
exercise STT is the trap for a long option left to expire in the money,
which the expiry sweep exists to prevent.

All amounts are in rupees; turnover is price x quantity (x lot units for
F&O, which Kite already counts in the quantity).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Segment = Literal["equity_delivery", "equity_intraday", "options"]

GST = 0.18
SEBI_PER_RUPEE = 10 / 1e7
NSE_TXN_EQUITY = 0.0000307
NSE_TXN_OPTIONS = 0.0003553
STT_DELIVERY = 0.001
STT_INTRADAY_SELL = 0.00025
STT_OPTIONS_SELL = 0.0015
STT_OPTIONS_EXERCISED = 0.0015
STAMP_DELIVERY_BUY = 0.00015
STAMP_INTRADAY_BUY = 0.00003
STAMP_OPTIONS_BUY = 0.00003
BROKERAGE_OPTIONS_PER_ORDER = 20.0
BROKERAGE_INTRADAY_RATE = 0.0003
BROKERAGE_INTRADAY_CAP = 20.0
DP_CHARGE_PER_SCRIP_SELL = 15.34


@dataclass(frozen=True)
class IndiaCosts:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange + self.sebi + self.stamp
                     + self.gst + self.dp, 2)


def round_trip(
    segment: Segment,
    *,
    buy_value: float,
    sell_value: float,
    exercised_intrinsic_value: float = 0.0,
) -> IndiaCosts:
    """Costs of one buy and one sell (one executed order each side).

    ``exercised_intrinsic_value`` is for a long option settled at expiry
    instead of sold: STT is then charged on the intrinsic value, not the
    sale. Pass sell_value=0 in that case.
    """
    turnover = buy_value + sell_value
    if segment == "equity_delivery":
        brokerage = 0.0
        stt = STT_DELIVERY * turnover
        exchange = NSE_TXN_EQUITY * turnover
        stamp = STAMP_DELIVERY_BUY * buy_value
        dp = DP_CHARGE_PER_SCRIP_SELL if sell_value > 0 else 0.0
    elif segment == "equity_intraday":
        brokerage = sum(
            min(BROKERAGE_INTRADAY_RATE * v, BROKERAGE_INTRADAY_CAP)
            for v in (buy_value, sell_value) if v > 0
        )
        stt = STT_INTRADAY_SELL * sell_value
        exchange = NSE_TXN_EQUITY * turnover
        stamp = STAMP_INTRADAY_BUY * buy_value
        dp = 0.0
    elif segment == "options":
        orders = (buy_value > 0) + (sell_value > 0)
        brokerage = BROKERAGE_OPTIONS_PER_ORDER * orders
        stt = STT_OPTIONS_SELL * sell_value + STT_OPTIONS_EXERCISED * exercised_intrinsic_value
        exchange = NSE_TXN_OPTIONS * turnover
        stamp = STAMP_OPTIONS_BUY * buy_value
        dp = 0.0
    else:
        raise ValueError(f"unknown segment {segment!r}")
    sebi = SEBI_PER_RUPEE * turnover
    gst = GST * (brokerage + sebi + exchange)
    return IndiaCosts(
        brokerage=round(brokerage, 2), stt=round(stt, 2), exchange=round(exchange, 2),
        sebi=round(sebi, 2), stamp=round(stamp, 2), gst=round(gst, 2), dp=round(dp, 2),
    )


def round_trip_pct(segment: Segment, *, buy_value: float, sell_value: float) -> float:
    """Round-trip cost as a percent of the amount bought: what a trade must
    make before it breaks even."""
    if buy_value <= 0:
        return 0.0
    return round(100.0 * round_trip(segment, buy_value=buy_value, sell_value=sell_value).total
                 / buy_value, 4)
