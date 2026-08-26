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
    uv run python -m trading_agents.jobs.daily_cron \\
        --user-id 00000000-0000-0000-0000-000000000001 \\
        --watchlist SPY,QQQ,AAPL,NVDA,MSFT,GOOG,AMZN,META,TSLA,JPM
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from engine.env import env_flag
from engine.scanner import ScanSignal
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


@dataclass(frozen=True)
class SymbolScanContext:
    """Why the scanner woke the council for one symbol, and where it ranks.

    Passed straight through to the analysts' feature dict so a triggered
    run can say WHICH deterministic condition fired rather than arriving
    with no more context than a scheduled sweep. Typed rather than a loose
    dict because this crosses the scanner → agents boundary.
    """

    signals: tuple[ScanSignal, ...] = ()
    relative_strength_rank: float | None = None


def _with_scan_context(
    provider: Any, scan_context: Mapping[str, SymbolScanContext]
) -> Any:
    """Wrap a feature provider so triggered runs carry their trigger reason.

    Wrapping rather than threading a parameter through ``run_council`` keeps
    the council's contract intact: agents still receive one pre-computed
    feature dict and still never fetch anything themselves.
    """

    async def _provider(symbol: str, horizon: str = "short") -> dict[str, Any]:
        features = provider(symbol, horizon)
        if inspect.isawaitable(features):
            features = await features
        ctx = scan_context.get(symbol.upper())
        if ctx is None:
            return features
        if ctx.signals:
            features["scan_triggers"] = [s.as_dict() for s in ctx.signals]
        quant = features.get("quant")
        if ctx.relative_strength_rank is not None and isinstance(quant, dict):
            quant["relative_strength_rank"] = ctx.relative_strength_rank
        return features

    return _provider


def _today_utc() -> str:
    """Date stamp for idempotency. UTC date is fine — the cron runs at a
    market-aligned hour, so different cron invocations on the same NYSE
    day map to the same UTC date in 99% of cases. Phase 1.5 swaps to NY
    business days via ``pandas_market_calendars``.
    """
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def _already_decided_today(
    user_id: str, symbol: str
) -> bool:
    """Has the council already produced a row for this (user, symbol) today?

    Uses the DecisionLog's indexed ``has_decision_today`` (an existence
    query on (user_id, symbol, triggered_at)) — NOT a full-history scan,
    so cron latency stays flat as the decision history grows.
    """
    return await get_decision_log().has_decision_today(
        user_id=user_id, symbol=symbol, day_utc=_today_utc()
    )


def _equity_resolver(user_id: str):
    """Latest reconciler-snapshot equity for the cron user (Postgres only).
    The sizer needs REAL equity — synthetic 100k sizing against a real
    account was audit finding §5."""

    async def _resolve() -> float | None:
        if not env_flag("USE_POSTGRES"):
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
    except Exception:  # push is best-effort, council result is already durable
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
    skip_calendar_gate: bool = False,
    skip_ghost_eval: bool = False,
    skip_reflect: bool = False,
    scan_context: Mapping[str, SymbolScanContext] | None = None,
) -> int:
    """Run the council across ``watchlist``. Returns a process exit code.

    ``scan_context`` is set by the continuous scanner: a triggered pass
    covers only the symbols that tripped a deterministic rule and forwards
    the rule identifiers to the analysts. A scheduled full sweep passes
    None and behaves exactly as before.

    ``force`` and ``skip_calendar_gate`` are deliberately independent
    knobs, not two spellings of the same thing:

    - ``force`` is the operator's "run it anyway" — it bypasses BOTH the
      market-calendar gate below AND the per-(user, symbol, day) dedup
      check in ``_run_one``. That is the CLI/human-operator contract
      (``--force``: "rerun this symbol right now, I know it already ran")
      and it must keep bypassing dedup.
    - ``skip_calendar_gate`` bypasses ONLY the calendar gate, for a caller
      that has already established the market is open by other means (the
      scanner's trigger loop checks ``ScanResult.market_open`` itself) and
      just wants this redundant check skipped. It does NOT touch the dedup
      check — a symbol already decided today, whether by the baseline
      sweep or an earlier trigger, is still suppressed. That is what keeps
      the once-per-symbol-per-day cap ``engine.scanner.cooldown``'s
      docstring promises true regardless of which loop woke the council.
    """
    log.info(
        "daily cron start — user=%s symbols=%s use_postgres=%s triggered=%s",
        user_id,
        ",".join(watchlist),
        env_flag("USE_POSTGRES"),
        bool(scan_context),
    )

    # Market-calendar gate: no NYSE close today → nothing to decide. The
    # GitHub Actions schedule fires Mon-Fri regardless of holidays; this is
    # the deterministic gate the audit asked for. --force overrides it (and
    # the dedup check below); skip_calendar_gate overrides ONLY this gate,
    # for a caller that already knows the market is open.
    today = datetime.now(UTC).date()
    from engine.features import is_us_trading_day

    if not (force or skip_calendar_gate) and not is_us_trading_day(today):
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

    if scan_context:
        feature_provider = _with_scan_context(feature_provider, scan_context)

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
        except Exception as exc:
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
    if not skip_ghost_eval and env_flag("USE_POSTGRES"):
        try:
            from trading_agents.jobs.ghost_eval import evaluate_ghosts

            await evaluate_ghosts()
        except Exception:
            log.exception("ghost_eval pass failed — continuing")

    # Reflection pass — the worker existed but was never scheduled, so
    # strategy-confidence priors never moved off 0.5 (audit roadmap P1).
    # Runs after ghost eval on the same closed-trade window; failure never
    # fails the cron. Skipped with --no-reflect.
    if not skip_reflect:
        try:
            from trading_agents.nodes import reflection_agent_run

            summary = await reflection_agent_run(
                llm=llm,
                decision_log=get_decision_log(),
                confidence_store=get_confidence_store(),
                since=timedelta(hours=24),
            )
            log.info("reflection pass — reviewed=%d", summary.get("reviewed", 0))
        except Exception:
            log.exception("reflection pass failed — continuing")

    # Kick the Langfuse export before this short-lived process exits.
    try:
        from trading_agents.tracing import flush as _trace_flush

        _trace_flush()
    except Exception:
        log.debug("trace flush failed", exc_info=True)

    return 1 if failed else 0


def cli() -> int:
    """Parse argv and run the daily pass. Returns a process exit code.

    Non-zero only when a symbol actually failed — a weekend/holiday
    short-circuit is a successful no-op, so cron does not alert on it.
    """
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
    parser.add_argument(
        "--no-reflect",
        action="store_true",
        help="Skip the EOD reflection pass (strategy-confidence update).",
    )
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    if not symbols:
        log.error("empty watchlist — pass --watchlist or set AGENT_CRON_WATCHLIST")
        return 2

    # The user's curated watchlist (user_watchlist table) overrides the
    # static default when it exists — that's the product: "tell the agent
    # what you're interested in, it tracks those."
    if env_flag("USE_POSTGRES"):
        try:
            user_symbols = asyncio.run(_load_user_watchlist(args.user_id))
        except Exception:  # fall back to the CLI/default list
            log.exception("user watchlist load failed — using default list")
            user_symbols = []
        if user_symbols:
            log.info("using user watchlist (%d symbols): %s",
                     len(user_symbols), ",".join(user_symbols))
            symbols = user_symbols

    return asyncio.run(
        main(
            args.user_id,
            symbols,
            force=args.force,
            skip_ghost_eval=args.skip_ghost_eval,
            skip_reflect=args.no_reflect,
        )
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
