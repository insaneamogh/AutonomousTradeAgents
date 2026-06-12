"""Daily council cron — runs the agent council across a watchlist.

Phase 4 paper-trading kickoff. PLAN.md §11 calls for daily decisions
across a small watchlist (start with 10 names: SPY, QQQ, AAPL, NVDA,
MSFT, GOOG, AMZN, META, TSLA, JPM). This script is the entry point.

Idempotency:
  - One ``DecisionEntry`` per (user, date_utc, symbol).
  - Re-runs within the same day are a no-op for symbols already
    decided that day. This makes the script safe for both
    cron-on-clock scheduling AND ad-hoc operator-fired retries.

Two ways to schedule this in production:

  1. **GitHub Actions** — `.github/workflows/daily_council.yml` with
     `schedule: - cron: '15 13 * * 1-5'` (13:15 UTC = 9:15 EST market
     open). Wires the secrets from the repo's secret store.

  2. **Fly machines** — `fly machine schedule` against this script.

See ``docs/RUNBOOK.md`` for the exact wiring snippets.

Usage:

    PYTHONPATH=apps/agents:packages/engine:packages/broker:apps/api \\
    USE_POSTGRES=1 \\
    uv run python -m apps.agents.scripts.daily_cron \\
        --user-id 00000000-0000-0000-0000-000000000001 \\
        --watchlist SPY,QQQ,AAPL,NVDA,MSFT,GOOG,AMZN,META,TSLA,JPM
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from trading_agents.features import resolve_feature_provider
from trading_agents.llm import LLM
from trading_agents.memory import get_confidence_store, get_decision_log
from trading_agents.runtime import run_council

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s — %(message)s",
)
log = logging.getLogger("agents.cron.daily")


# Default watchlist — kept short on purpose. PLAN.md §11 hardens this
# to a per-user persisted list later; for kickoff a static set is fine.
DEFAULT_WATCHLIST: tuple[str, ...] = (
    "SPY", "QQQ", "AAPL", "NVDA", "MSFT",
    "GOOG", "AMZN", "META", "TSLA", "JPM",
)


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def _today_utc() -> str:
    """Date stamp for idempotency. UTC date is fine — the cron runs at a
    market-aligned hour, so different cron invocations on the same NYSE
    day map to the same UTC date in 99% of cases. Phase 1.5 swaps to NY
    business days via ``pandas_market_calendars``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _already_decided_today(
    user_id: str, symbol: str
) -> bool:
    """Has the council already produced a row for this (user, symbol) today?

    Reads through the DecisionLog. The in-memory log forgets across
    restarts, which is fine for dev; the Postgres log is the source of
    truth in prod.
    """
    log_ = get_decision_log()
    today = _today_utc()
    decisions = await log_.all_decisions()
    for d in decisions:
        if d.user_id != user_id:
            continue
        if d.symbol != symbol:
            continue
        if d.triggered_at.strftime("%Y-%m-%d") == today:
            return True
    return False


def _equity_resolver(user_id: str):
    """Latest reconciler-snapshot equity for the cron user (Postgres only).
    The sizer needs REAL equity — synthetic 100k sizing against a real
    account was audit finding §5."""

    async def _resolve() -> float | None:
        if not _is_truthy(os.environ.get("USE_POSTGRES")):
            return None
        import uuid as _uuid

        from sqlalchemy import desc, select

        from engine.db.models import PositionsSnapshot
        from engine.db.session import async_session_factory

        factory = async_session_factory()
        async with factory() as session:
            stmt = (
                select(PositionsSnapshot.account_equity)
                .where(PositionsSnapshot.user_id == _uuid.UUID(user_id))
                .order_by(desc(PositionsSnapshot.captured_at))
                .limit(1)
            )
            equity = (await session.execute(stmt)).scalar_one_or_none()
        return float(equity) if equity is not None else None

    return _resolve


def _notify_proposal(user_id: str, proposal: dict, push_tasks: list) -> None:
    """Fan out the 'new proposal' push. The audit's Break 4: cron proposals
    never notified anyone and expired unseen. Failure never fails the cron."""
    try:
        from app.services.notifications import schedule_proposal_pending_notification

        push_tasks.append(
            schedule_proposal_pending_notification(user_id=user_id, proposal=proposal)
        )
    except Exception:  # noqa: BLE001 — push is best-effort, council result is already durable
        log.exception("proposal push fan-out failed — continuing")


async def _run_one(
    user_id: str,
    symbol: str,
    llm: LLM,
    *,
    force: bool,
    feature_provider,
    push_tasks: list,
) -> dict:
    """Run the council for a single symbol. Skips if already decided
    today unless ``force=True``.
    """
    if not force and await _already_decided_today(user_id, symbol):
        log.info("skip %s — already decided today", symbol)
        return {"symbol": symbol, "skipped": True}

    result = await run_council(
        symbol=symbol,
        user_id=user_id,
        llm=llm,
        feature_provider=feature_provider,
        decision_log=get_decision_log(),
        confidence_store=get_confidence_store(),
    )
    log.info(
        "%s: final_action=%s strategy=%s confidence=%.2f decision_id=%s",
        symbol,
        result.get("final_action"),
        result.get("selected_strategy"),
        result.get("selector_confidence", 0.0),
        result.get("decision_id"),
    )
    if result.get("proposal") is not None:
        _notify_proposal(user_id, result["proposal"], push_tasks)
    return {
        "symbol": symbol,
        "skipped": False,
        "final_action": result.get("final_action"),
        "selected_strategy": result.get("selected_strategy"),
        "decision_id": result.get("decision_id"),
    }


async def main(
    user_id: str,
    watchlist: list[str],
    *,
    force: bool,
    skip_ghost_eval: bool = False,
) -> int:
    log.info(
        "daily cron start — user=%s symbols=%s use_postgres=%s",
        user_id,
        ",".join(watchlist),
        _is_truthy(os.environ.get("USE_POSTGRES")),
    )

    # Market-calendar gate: no NYSE close today → nothing to decide. The
    # GitHub Actions schedule fires Mon-Fri regardless of holidays; this is
    # the deterministic gate the audit asked for. --force overrides.
    today = datetime.now(timezone.utc).date()
    from engine.features import is_us_trading_day

    if not force and not is_us_trading_day(today):
        log.info("US market closed on %s — skipping council run", today)
        return 0

    # Both constructors hard-fail under the REQUIRE flags — a misconfigured
    # production cron must crash loudly, never degrade to mock/synthetic.
    try:
        llm = LLM()
        feature_provider = resolve_feature_provider(
            equity_resolver=_equity_resolver(user_id)
        )
    except RuntimeError:
        log.exception("daily cron refused to start (REQUIRE flag failed)")
        return 2
    log.info("LLM mode: %s", "MOCK" if llm.mock else "REAL")

    push_tasks: list = []
    rolled_up: list[dict] = []
    # Sequential — Anthropic prompt-caching benefits from steady cadence
    # within ~30s windows. Parallel would burn separate cache entries.
    for symbol in watchlist:
        try:
            rolled_up.append(
                await _run_one(
                    user_id, symbol, llm,
                    force=force,
                    feature_provider=feature_provider,
                    push_tasks=push_tasks,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("council failed for %s — continuing", symbol)
            rolled_up.append({"symbol": symbol, "skipped": False, "error": str(exc)})

    # Push fan-outs are fire-and-forget tasks — drain them before the
    # process exits or the notifications die with the event loop.
    if push_tasks:
        results = await asyncio.gather(*push_tasks, return_exceptions=True)
        sent = sum(1 for r in results if not isinstance(r, Exception))
        log.info("proposal pushes drained — %d/%d ok", sent, len(push_tasks))

    processed = sum(1 for r in rolled_up if not r.get("skipped") and "error" not in r)
    skipped = sum(1 for r in rolled_up if r.get("skipped"))
    failed = sum(1 for r in rolled_up if "error" in r)
    log.info(
        "daily cron done — processed=%d skipped=%d failed=%d",
        processed, skipped, failed,
    )

    # Ghost P&L pass — marks vetoed/declined picks against daily closes.
    # Postgres only (the ghost_outcomes table); failure never fails cron.
    if not skip_ghost_eval and _is_truthy(os.environ.get("USE_POSTGRES")):
        try:
            from scripts.ghost_eval import evaluate_ghosts

            await evaluate_ghosts()
        except Exception:  # noqa: BLE001
            log.exception("ghost_eval pass failed — continuing")

    return 1 if failed else 0


def cli() -> int:
    parser = argparse.ArgumentParser(description="Daily council cron.")
    parser.add_argument(
        "--user-id",
        default=os.environ.get(
            "AGENT_CRON_USER_ID",
            "00000000-0000-0000-0000-000000000001",
        ),
        help="User ID to attribute decisions to. Defaults to the fixture user.",
    )
    parser.add_argument(
        "--watchlist",
        default=os.environ.get("AGENT_CRON_WATCHLIST", ",".join(DEFAULT_WATCHLIST)),
        help="Comma-separated tickers.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even when a decision already exists for (user, symbol) today.",
    )
    parser.add_argument(
        "--skip-ghost-eval",
        action="store_true",
        help="Skip the ghost-P&L marking pass after the council loop.",
    )
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    if not symbols:
        log.error("empty watchlist — pass --watchlist or set AGENT_CRON_WATCHLIST")
        return 2

    # The user's curated watchlist (user_watchlist table) overrides the
    # static default when it exists — that's the product: "tell the agent
    # what you're interested in, it tracks those."
    if _is_truthy(os.environ.get("USE_POSTGRES")):
        try:
            user_symbols = asyncio.run(_load_user_watchlist(args.user_id))
        except Exception:  # noqa: BLE001 — fall back to the CLI/default list
            log.exception("user watchlist load failed — using default list")
            user_symbols = []
        if user_symbols:
            log.info("using user watchlist (%d symbols): %s",
                     len(user_symbols), ",".join(user_symbols))
            symbols = user_symbols

    return asyncio.run(
        main(args.user_id, symbols, force=args.force, skip_ghost_eval=args.skip_ghost_eval)
    )


async def _load_user_watchlist(user_id: str) -> list[str]:
    """Active user_watchlist symbols, alphabetical. Empty when uncurated."""
    import uuid as _uuid

    from sqlalchemy import select

    from engine.db.models import UserWatchlistItem
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        stmt = (
            select(UserWatchlistItem.symbol)
            .where(UserWatchlistItem.user_id == _uuid.UUID(user_id))
            .where(UserWatchlistItem.active.is_(True))
            .order_by(UserWatchlistItem.symbol)
        )
        rows = (await session.execute(stmt)).scalars().all()
    return [str(s).upper() for s in rows]


if __name__ == "__main__":
    sys.exit(cli())
