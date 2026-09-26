"""NSE option chain from Kite Connect, as ``ContractQuote`` candidates.

docs/PLAN_ZERODHA.md Z2. Alpaca's chain snapshot hands the drafter bid,
ask, delta, IV and open interest per contract. Kite has no chain endpoint
and returns no greeks, so the chain is assembled here:

  1. the NFO instruments dump (cached per IST day): name, expiry, strike,
     CE/PE and lot size for every listed contract;
  2. the contracts of the wanted type inside the DTE window and near the
     money, ranked nearest-the-money first, capped so the underlying and
     all of them fit ONE /quote call (Kite: 500 instruments per call, and
     about one quote call per second);
  3. IV from the quote's bid/ask mid by Black-Scholes, and delta from that
     IV. NSE index and stock options are European, so BS is the right
     model, not an approximation of an American one.

Units. Kite counts F&O quantity in units (lots x lot size) in orders,
positions and depth. Open interest and volume are converted to LOTS here
so selection's contract-count floors (open interest >= 100) mean for an
NFO contract what they mean for a US one. Kite's docs do not state the
unit of ``oi``/``volume``; if they are already contracts, dividing
understates liquidity and refuses more, which is the safe direction.
Unverified against a live account.

``ContractQuote.multiplier`` carries the lot size, so the premium sizer
sizes in lots; the drafter then writes the order quantity in units with
multiplier 1, which is how Kite and the risk rules count it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from engine.options import pricing
from engine.options.selection import ContractQuote
from engine.risk.markets import tradingsymbol_of
from engine.risk.types import RiskCaps

logger = logging.getLogger("engine.options.kite_chain")

_IST = ZoneInfo("Asia/Kolkata")

ClientFactory = Callable[[], AbstractAsyncContextManager[Any]]

kite_client_factory: ContextVar[ClientFactory | None] = ContextVar(
    "kite_client_factory", default=None
)
"""Opens the current user's Kite client. Set by the daily cron around one
council run for an Indian symbol; None anywhere else, which leaves an NSE
option draft with no candidates (a named HOLD), never an Alpaca call."""

INDEX_OPTION_NAMES: dict[str, str] = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
}
"""NSE index tradingsymbol -> the ``name`` its NFO options carry in the
instruments dump. A stock's options carry the stock's own tradingsymbol."""

STRIKE_WINDOW_PCT = 6.0
"""Strikes within this percent of spot. Roughly one standard deviation of
a month's NIFTY move, which covers every delta band selection can ask for
while keeping the contract count inside one quote call."""

MAX_QUOTED_CONTRACTS = 499
"""One /quote call carries 500 instruments; one slot is the underlying."""

CARRY_RATE = 0.05
"""Risk-free rate less dividend yield, as a decimal: about a 6.5% T-bill
less NIFTY's ~1.3% yield. At 10-45 DTE, delta barely moves with it."""

EXPIRY_CUTOFF = time(15, 30)
"""NSE options expire at the close on expiry day."""

_dump: dict[str, tuple[date, dict[str, list[tuple[str, date, float, str, int]]]]] = {}


def option_name_for(underlying: str) -> str:
    """``NSE:NIFTY 50`` -> ``NIFTY``; ``NSE:RELIANCE`` -> ``RELIANCE``."""
    ts = tradingsymbol_of(underlying).upper()
    return INDEX_OPTION_NAMES.get(ts, ts)


def reset_cache_for_tests() -> None:
    _dump.clear()


async def _nfo_options(client: Any, today: date) -> dict[str, list[tuple[str, date, float, str, int]]]:
    """Every NFO option as (tradingsymbol, expiry, strike, CE/PE, lot size),
    grouped by name. The full dump is ~90k rows; keeping only these
    tuples keeps a day's cache small."""
    cached = _dump.get("NFO")
    if cached is not None and cached[0] == today:
        return cached[1]
    by_name: dict[str, list[tuple[str, date, float, str, int]]] = {}
    for r in await client.instruments("NFO"):
        kind = str(r.get("instrument_type", "")).upper()
        if kind not in ("CE", "PE"):
            continue
        try:
            expiry = date.fromisoformat(str(r["expiry"])[:10])
        except (KeyError, ValueError):
            continue
        by_name.setdefault(str(r.get("name", "")).upper(), []).append((
            str(r["tradingsymbol"]).upper(), expiry, float(r.get("strike") or 0.0), kind,
            int(r.get("lot_size") or 0),
        ))
    _dump["NFO"] = (today, by_name)
    logger.info("kite_chain: %d option names cached for %s", len(by_name), today)
    return by_name


def _quote_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return ts.replace(tzinfo=_IST) if ts.tzinfo is None else ts


def _best(depth: dict[str, Any] | None, side: str) -> float | None:
    levels = (depth or {}).get(side) or []
    price = float(levels[0].get("price") or 0.0) if levels else 0.0
    return price if price > 0 else None


def _years_to_expiry(expiry: date, now: datetime) -> float:
    close = datetime.combine(expiry, EXPIRY_CUTOFF, tzinfo=_IST)
    return max(0.0, (close - now).total_seconds()) / (365.0 * 86400.0)


async def fetch_kite_option_candidates(
    underlying: str,
    *,
    client: Any,
    now: datetime,
    contract_type: str | None = None,
    spot_hint: float | None = None,
    caps: RiskCaps | None = None,
) -> tuple[ContractQuote, ...]:
    """``ContractQuote`` candidates for an NSE underlying, ready for
    ``engine.options.selection.select_contract``.

    ``contract_type`` ("call"/"put") and ``spot_hint`` (the last close the
    council already has) narrow the contracts before quoting, so the
    underlying and its candidates fit one quote call. Without a hint the
    spot is quoted first, one extra call. Returns () when Kite lists no
    options for the underlying or the spot cannot be read.
    """
    resolved = caps or RiskCaps.from_env()
    today = now.astimezone(_IST).date()
    options = (await _nfo_options(client, today)).get(option_name_for(underlying), [])
    if not options:
        logger.info("kite_chain: no NFO options listed for %s", underlying)
        return ()

    spot = spot_hint
    if spot is None or spot <= 0:
        spot = float((await client.quotes([underlying])).get(underlying.upper(), {})
                     .get("last_price") or 0.0)
    if spot <= 0:
        logger.warning("kite_chain: no spot for %s", underlying)
        return ()

    kinds = {"call": ("CE",), "put": ("PE",)}.get(str(contract_type or ""), ("CE", "PE"))
    lo_day = today + timedelta(days=resolved.options_min_dte)
    hi_day = today + timedelta(days=resolved.options_max_dte)
    window = STRIKE_WINDOW_PCT / 100.0
    wanted = [
        o for o in options
        if o[3] in kinds and lo_day <= o[1] <= hi_day and o[4] > 0
        and abs(o[2] / spot - 1.0) <= window
    ]
    wanted.sort(key=lambda o: (abs(math.log(o[2] / spot)), o[1]))
    wanted = wanted[:MAX_QUOTED_CONTRACTS]
    if not wanted:
        return ()

    quotes = await client.quotes([underlying, *(f"NFO:{o[0]}" for o in wanted)])
    live_spot = float((quotes.get(underlying.upper()) or {}).get("last_price") or 0.0)
    if live_spot > 0:
        spot = live_spot

    out: list[ContractQuote] = []
    now_utc = now.astimezone(UTC)
    for ts, expiry, strike, kind, lot in wanted:
        q = quotes.get(f"NFO:{ts}")
        if not q:
            continue
        bid, ask = _best(q.get("depth"), "buy"), _best(q.get("depth"), "sell")
        side = "call" if kind == "CE" else "put"
        iv = delta = None
        t = _years_to_expiry(expiry, now_utc)
        if bid is not None and ask is not None and ask >= bid and t > 0:
            iv = pricing.implied_vol(
                (bid + ask) / 2.0, spot, strike, t, kind=side, rate=CARRY_RATE
            )
            if iv is not None:
                delta = pricing.delta(spot, strike, t, iv, kind=side, rate=CARRY_RATE)
        oi, volume = q.get("oi"), q.get("volume")
        out.append(ContractQuote(
            occ_symbol=f"NFO:{ts}",
            contract_type=side,
            strike=strike,
            expiry=expiry,
            bid=bid,
            ask=ask,
            open_interest=int(float(oi)) // lot if oi is not None else None,
            volume=int(float(volume)) // lot if volume is not None else None,
            delta=delta,
            implied_volatility=iv,
            quote_ts=_quote_ts(q.get("timestamp") or q.get("last_trade_time")),
            multiplier=lot,
        ))
    return tuple(out)
