"""Record one constant-maturity ATM IV snapshot per options underlying per day.

docs/PLAN_PLATFORM.md §D P1. `options_context.iv_rank`, `atm_iv` and
`term_structure_slope` stay None until there is history to rank against,
and the free Alpaca tier cannot supply history retroactively. So this runs
once a day from the council scheduler and writes `iv_history`. It is
read-only against the broker (chain snapshots only), makes no LLM calls,
and costs one chain request per underlying.

Every symbol is independent: one symbol's failure is logged and counted,
never raised, so a bad chain cannot cost the rest of the day's record.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from engine.options.iv_surface import atm_iv_by_expiry, constant_maturity_iv

logger = logging.getLogger("agents.jobs.iv_snapshot")

_MAX_EXPIRY_DAYS = 120


@dataclass(frozen=True)
class IvRow:
    symbol: str
    day: date
    atm_iv_30d: float | None
    atm_iv_60d: float | None
    n_expiries: int
    feed: str


ChainFetcher = Callable[[str, date], Awaitable[Iterable[Any]]]
RowWriter = Callable[[list[IvRow]], Awaitable[None]]


def build_row(symbol: str, today: date, quotes: Iterable[Any], feed: str) -> IvRow:
    points = atm_iv_by_expiry(quotes, today)
    return IvRow(
        symbol=symbol.upper(),
        day=today,
        atm_iv_30d=constant_maturity_iv(points, 30),
        atm_iv_60d=constant_maturity_iv(points, 60),
        n_expiries=len(points),
        feed=feed,
    )


async def snapshot(
    symbols: Iterable[str],
    today: date,
    *,
    fetch_chain: ChainFetcher,
    write_rows: RowWriter,
    feed: str,
) -> dict[str, int]:
    rows: list[IvRow] = []
    failed = empty = 0
    for sym in sorted({s.upper() for s in symbols if s}):
        try:
            quotes = list(await fetch_chain(sym, today))
        except Exception as exc:
            logger.warning("iv_snapshot: chain fetch failed for %s — %s", sym, type(exc).__name__)
            failed += 1
            continue
        row = build_row(sym, today, quotes, feed)
        if row.atm_iv_30d is None and row.atm_iv_60d is None:
            empty += 1  # recorded anyway: "no ATM market today" is a fact too
        rows.append(row)
    if rows:
        await write_rows(rows)
    return {"recorded": len(rows), "no_atm_iv": empty, "failed": failed}


# ── production wiring ────────────────────────────────────────────────


def alpaca_chain_fetcher(api_key: str, secret_key: str) -> ChainFetcher:
    async def _fetch(symbol: str, today: date) -> Iterable[Any]:
        from broker.alpaca import list_option_chain_quotes

        return await list_option_chain_quotes(
            symbol,
            api_key=api_key,
            secret_key=secret_key,
            expiration_date_gte=today + timedelta(days=7),
            expiration_date_lte=today + timedelta(days=_MAX_EXPIRY_DAYS),
        )

    return _fetch


KITE_QUOTE_SPACING_S = 1.1
"""Kite allows about one quote call per second; the chain fetch makes one
or two, so underlyings are spaced out rather than fired together."""

INDIA_VIX = "NSE:INDIA VIX"


def kite_chain_fetcher(client_factory: Callable[[], Any]) -> ChainFetcher:
    """The NSE chain through the user's Kite client (docs/PLAN_ZERODHA.md
    Z2): calls and puts near the money, 7-120 DTE, IV and delta computed
    from the quote mid (engine.options.kite_chain)."""
    async def _fetch(symbol: str, today: date) -> Iterable[Any]:
        import asyncio
        from dataclasses import replace
        from datetime import UTC, datetime

        from engine.options.kite_chain import fetch_kite_option_candidates
        from engine.risk.types import RiskCaps

        caps = replace(RiskCaps.from_env(), options_min_dte=7, options_max_dte=_MAX_EXPIRY_DAYS)
        try:
            async with client_factory() as client:
                return await fetch_kite_option_candidates(
                    symbol, client=client, now=datetime.now(UTC), caps=caps,
                )
        finally:
            await asyncio.sleep(KITE_QUOTE_SPACING_S)

    return _fetch


async def india_vix_row(client_factory: Callable[[], Any], today: date) -> IvRow | None:
    """India VIX as its own row: NSE's 30-day implied volatility of NIFTY,
    quoted in percent, stored as a decimal in atm_iv_30d like every other
    row. None when Kite does not quote it."""
    async with client_factory() as client:
        quote = (await client.quotes([INDIA_VIX])).get(INDIA_VIX) or {}
    try:
        vix = float(quote.get("last_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if vix <= 0:
        return None
    return IvRow(symbol=INDIA_VIX, day=today, atm_iv_30d=round(vix / 100.0, 5),
                 atm_iv_60d=None, n_expiries=0, feed="kite")


async def postgres_writer(rows: list[IvRow]) -> None:
    """Upsert on (symbol, day): a same-day re-run replaces, never duplicates."""
    from sqlalchemy.dialects.postgresql import insert

    from engine.db.models import IvHistory
    from engine.db.session import async_session_factory

    values = [
        {
            "symbol": r.symbol, "day": r.day, "atm_iv_30d": r.atm_iv_30d,
            "atm_iv_60d": r.atm_iv_60d, "n_expiries": r.n_expiries, "feed": r.feed,
        }
        for r in rows
    ]
    stmt = insert(IvHistory).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[IvHistory.symbol, IvHistory.day],
        set_={
            "atm_iv_30d": stmt.excluded.atm_iv_30d,
            "atm_iv_60d": stmt.excluded.atm_iv_60d,
            "n_expiries": stmt.excluded.n_expiries,
            "feed": stmt.excluded.feed,
            "captured_at": stmt.excluded.captured_at,
        },
    )
    factory = async_session_factory()
    async with factory() as session:
        await session.execute(stmt)
        await session.commit()
