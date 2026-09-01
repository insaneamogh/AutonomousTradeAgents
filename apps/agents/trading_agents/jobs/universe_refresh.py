"""Universe screener — Alpaca's full tradable universe, narrowed to a real
candidate list and written into ``user_watchlist``'s auto-discovered tier.

The scanner has only ever swept the user's own hand-curated watchlist (45
symbols as of 2026-09-01) — never Alpaca's full ~13.4k-symbol tradable
universe, even though the broker itself already exposes everything needed
to screen it for free (``packages/broker/broker/alpaca.py``'s
``list_tradable_assets``/``list_most_active_symbols``, both already used
elsewhere — the Settings ticker typeahead — just never for scanner
discovery).

Two Alpaca signals combined, neither sufficient alone:
  - ``list_most_active_symbols`` — real-time share-volume ranking (WHAT is
    trading right now). Alone, this surfaces penny-stock noise: a sub-$1
    ticker trades huge share counts precisely because it's cheap, not
    because it's a quality name.
  - ``list_tradable_assets`` — the broker's own tradable/fractionable/
    has_options verdict per symbol (WHETHER it's a real, tradable name).
    Alone, this carries no activity ranking at all — 13.4k undifferentiated
    rows.

``fractionable`` is the quality filter: Alpaca only enables fractional
trading on a curated, liquid, actively-traded subset (~7.6k of 13.4k
tradable names, measured live 2026-09-01) — a real signal Alpaca computes
about the name, not a threshold invented here.

Deliberately capped, not "screen literally everything": running the full
council (an LLM pass) on thousands of symbols every scan is not something
this system's cost/rate-limit budget survives, and `SCANNER_MAX_COUNCIL_RUNS`
already exists specifically to bound that. This job feeds the scanner a
much richer CANDIDATE pool to pick its already-capped per-pass runs from —
it does not remove or need to remove that cap.

Writes into ``user_watchlist.source='auto'`` — see migration
0018_watchlist_source. Never touches or deletes a ``source='manual'`` row,
and never duplicates a symbol the user already added by hand (checked
before insert, since ``(user_id, symbol)`` is uniquely constrained
regardless of source).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import uuid as _uuid

log = logging.getLogger("agents.jobs.universe_refresh")

DEFAULT_EQUITY_CANDIDATES = 100
"""Auto-discovered equity symbols kept per refresh. Roughly doubles the
current ~37-symbol manually-curated equity watchlist — a meaningful
expansion, deliberately not "all 13.4k": strategy_fit still has to fetch
real bars/technicals for every watchlist symbol every scan pass, and that
cost scales with watchlist size regardless of how few ever reach an LLM
call."""

DEFAULT_OPTIONS_CANDIDATES = 15
"""Auto-discovered options-eligible symbols kept per refresh. Smaller than
the equity cap on purpose: each one that clears strategy_fit's floor
triggers a full two-agent (Bull/Bear) options council pass, roughly twice
the LLM cost of an equity pass."""

DEFAULT_ACTIVITY_POOL = 100
"""Passed through to `list_most_active_symbols` as its per-ranking `top` —
Alpaca's screener endpoint rejects anything over 100 outright ("invalid
top: should not be larger than 100", confirmed live). Since that function
merges TWO independent rankings (volume, trade count), the real pool size
after dedup is typically closer to 150-200, not 100 — comfortably more
than max_equity + max_options so the fractionable/has_options filter has
real headroom to work with."""


async def screen_universe(
    *,
    api_key: str,
    secret_key: str,
    max_equity: int = DEFAULT_EQUITY_CANDIDATES,
    max_options: int = DEFAULT_OPTIONS_CANDIDATES,
    activity_pool: int = DEFAULT_ACTIVITY_POOL,
) -> tuple[list[str], list[str]]:
    """Returns ``(equity_symbols, options_symbols)`` — both real,
    broker-verified tradable names, both already ranked by real trading
    activity (Alpaca's own ranking, not recomputed here). An options
    symbol is also included in ``equity_symbols`` only if it independently
    ranks within ``max_equity`` — the two lists are not required to be
    disjoint, and the caller (``refresh_watchlist``) is responsible for
    not writing a duplicate row for a symbol that lands in both.
    """
    from broker.alpaca import list_most_active_symbols, list_tradable_assets

    active_symbols = await list_most_active_symbols(
        api_key=api_key, secret_key=secret_key, top=activity_pool
    )
    tradable = await list_tradable_assets(api_key=api_key, secret_key=secret_key)
    by_symbol = {a.symbol: a for a in tradable}

    equity: list[str] = []
    options: list[str] = []
    for sym in active_symbols:  # already rank-ordered by activity, best first
        asset = by_symbol.get(sym)
        if asset is None or not asset.tradable or not asset.fractionable:
            continue
        if len(equity) < max_equity:
            equity.append(sym)
        if asset.has_options and len(options) < max_options:
            options.append(sym)
        if len(equity) >= max_equity and len(options) >= max_options:
            break

    return equity, options


async def refresh_watchlist(
    user_id: str,
    *,
    api_key: str,
    secret_key: str,
    max_equity: int = DEFAULT_EQUITY_CANDIDATES,
    max_options: int = DEFAULT_OPTIONS_CANDIDATES,
) -> dict[str, int]:
    """Replace this user's ``source='auto'`` watchlist rows with a freshly
    screened set. Never touches a ``source='manual'`` row, and skips any
    symbol the user already has under EITHER source — a manual pick always
    wins, and a symbol can't have two rows under the same (user_id, symbol)
    unique constraint regardless of source.
    """
    equity, options = await screen_universe(
        api_key=api_key,
        secret_key=secret_key,
        max_equity=max_equity,
        max_options=max_options,
    )

    from sqlalchemy import delete, select

    from engine.db.models import UserWatchlistItem
    from engine.db.session import async_session_factory

    uid = _uuid.UUID(user_id)
    option_set = set(options)

    factory = async_session_factory()
    async with factory() as session:
        # Existing rows under ANY source, read fresh in this same
        # transaction — a manual row added between screens must still win.
        existing = set(
            (
                await session.execute(
                    select(UserWatchlistItem.symbol).where(
                        UserWatchlistItem.user_id == uid,
                        UserWatchlistItem.source == "manual",
                    )
                )
            )
            .scalars()
            .all()
        )

        candidates = [sym for sym in {*equity, *options} if sym not in existing]
        rows = [
            UserWatchlistItem(
                user_id=uid,
                symbol=sym,
                asset_class="option" if sym in option_set else "equity",
                active=True,
                source="auto",
            )
            for sym in candidates
        ]

        await session.execute(
            delete(UserWatchlistItem).where(
                UserWatchlistItem.user_id == uid,
                UserWatchlistItem.source == "auto",
            )
        )
        session.add_all(rows)
        await session.commit()

    n_options_written = sum(1 for r in rows if r.asset_class == "option")
    log.info(
        "universe_refresh: %d auto-discovered rows written for user %s "
        "(%d options-eligible), %d skipped as already manually tracked",
        len(rows),
        user_id,
        n_options_written,
        len({*equity, *options}) - len(candidates),
    )
    return {"equity": len(rows) - n_options_written, "options": n_options_written}


def cli() -> int:
    """Parse argv and run one refresh pass. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Refresh the auto-discovered universe screen.")
    parser.add_argument(
        "--user-id",
        default=os.environ.get("AGENT_CRON_USER_ID", "00000000-0000-0000-0000-000000000001"),
    )
    parser.add_argument("--max-equity", type=int, default=DEFAULT_EQUITY_CANDIDATES)
    parser.add_argument("--max-options", type=int, default=DEFAULT_OPTIONS_CANDIDATES)
    args = parser.parse_args()

    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        log.error("ALPACA_API_KEY/ALPACA_SECRET_KEY not set — cannot screen the universe")
        return 2

    result = asyncio.run(
        refresh_watchlist(
            args.user_id,
            api_key=api_key,
            secret_key=secret_key,
            max_equity=args.max_equity,
            max_options=args.max_options,
        )
    )
    log.info("universe_refresh done: %s", result)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(cli())
