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


async def _run_one(user_id: str, symbol: str, llm: LLM, *, force: bool) -> dict:
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
    return {
        "symbol": symbol,
        "skipped": False,
        "final_action": result.get("final_action"),
        "selected_strategy": result.get("selected_strategy"),
        "decision_id": result.get("decision_id"),
    }


async def main(user_id: str, watchlist: list[str], *, force: bool) -> int:
    log.info(
        "daily cron start — user=%s symbols=%s use_postgres=%s",
        user_id,
        ",".join(watchlist),
        _is_truthy(os.environ.get("USE_POSTGRES")),
    )
    llm = LLM()
    log.info("LLM mode: %s", "MOCK" if llm.mock else "REAL")

    rolled_up: list[dict] = []
    # Sequential — Anthropic prompt-caching benefits from steady cadence
    # within ~30s windows. Parallel would burn separate cache entries.
    for symbol in watchlist:
        try:
            rolled_up.append(await _run_one(user_id, symbol, llm, force=force))
        except Exception as exc:  # noqa: BLE001
            log.exception("council failed for %s — continuing", symbol)
            rolled_up.append({"symbol": symbol, "skipped": False, "error": str(exc)})

    processed = sum(1 for r in rolled_up if not r.get("skipped") and "error" not in r)
    skipped = sum(1 for r in rolled_up if r.get("skipped"))
    failed = sum(1 for r in rolled_up if "error" in r)
    log.info(
        "daily cron done — processed=%d skipped=%d failed=%d",
        processed, skipped, failed,
    )
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
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    if not symbols:
        log.error("empty watchlist — pass --watchlist or set AGENT_CRON_WATCHLIST")
        return 2
    return asyncio.run(main(args.user_id, symbols, force=args.force))


if __name__ == "__main__":
    sys.exit(cli())
