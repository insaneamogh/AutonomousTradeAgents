"""Alpaca option-contract daily bars — the ghost mark for an OPTION.

Why this exists at all: ``engine.prices.alpaca`` marks against *stock*
bars. Passing an OCC symbol (``NVDA260918C00250000``) to the stock-bars
endpoint returns an empty series, not an error — so before this module,
every options ghost silently skipped and the Refusal Ledger showed
nothing for exactly the instrument the hackathon requires.

An option's P&L is the PREMIUM's move, not the underlying's. A refused
$2.17 call that goes to $3.40 made $123/contract, not "NVDA moved $4".
Marking the underlying would be a different (and wrong) number.

Feed note: the free/paper plan serves the OPRA *indicative* feed with a
15-minute delay. Daily closes are unaffected by intraday delay. No
``feed=`` argument is passed — unlike the stock client, the options data
API has no SIP/IEX split to disambiguate.

**Sparsity is expected and is not a bug.** Verified live 2026-08-30: a
3-contract SPY request over 10 days returned bars for 2 contracts on 3
days total — an illiquid strike simply does not print every session. The
ghost evaluator therefore marks only the offsets that HAVE a bar, and
finalizes on ELAPSED trading days rather than on the last day that
printed one; keying on the latter would leave options ghosts "partial"
forever and the ledger empty.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta

from engine.prices.base import DailyClose

logger = logging.getLogger("engine.prices.option_alpaca")

# The free/paper options entitlement serves data older than 15 minutes.
# 20 gives clock-skew headroom without costing a bar: these are DAILY
# bars, so trimming the last 20 minutes of the current session never
# removes a completed close.
_DELAY_MARGIN = timedelta(minutes=20)


class AlpacaOptionPriceProvider:
    """Daily closes for one OCC contract symbol.

    Satisfies the same ``PriceProvider`` protocol as the stock provider so
    ``ghost_eval`` can swap one for the other with no branching beyond
    "is this proposal an option".
    """

    name = "alpaca_option"

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient

            self._client = OptionHistoricalDataClient(self._api_key, self._secret_key)
        return self._client

    async def daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame

        occ = symbol.upper()
        # ``end`` is clamped out of the delayed window. Asking for
        # 23:59:59 of the current day is asking for data inside the
        # 15-minute delay, which the free plan rejects with a 403 whose
        # message is actively misleading:
        #
        #   end=2026-08-30T23:59:59Z -> 403 "OPRA agreement is not signed"
        #   end=2026-08-29T00:00:00Z -> 200, bars returned
        #
        # Same keys, same contract, seconds apart (verified 2026-08-30).
        # Nothing is wrong with the agreement; the request simply reaches
        # past what the entitlement serves. Without this clamp EVERY ghost
        # pass 403s, since the evaluator always marks up to ``today``.
        requested_end = datetime.combine(end, time.max, tzinfo=UTC)
        latest_servable = datetime.now(UTC) - _DELAY_MARGIN
        end_dt = min(requested_end, latest_servable)
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        if end_dt < start_dt:
            return []
        req = OptionBarsRequest(
            symbol_or_symbols=occ,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
        )
        try:
            # alpaca-py is sync — thread it so the evaluator stays async,
            # same as the stock provider.
            bars = await asyncio.to_thread(self._get_client().get_option_bars, req)
        except Exception:
            # A contract that has expired, or one the plan cannot serve,
            # must not abort the whole ghost pass for every other row.
            logger.warning("option bars unavailable for %s", occ, exc_info=True)
            return []
        data = bars.data.get(occ, [])
        return [DailyClose(day=b.timestamp.date(), close=float(b.close)) for b in data]
