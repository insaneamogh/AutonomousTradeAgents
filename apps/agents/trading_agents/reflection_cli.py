"""One-shot CLI for the Reflection Agent.

    uv run python -m trading_agents.reflection_cli --since 24h

Runs the Reflection loop over a synthetic seed of decisions for a demo.
The real wiring (production reflection job over PostgresDecisionLog) lands
when the API picks up the auth layer in Phase 3; for now the CLI proves
the contract end-to-end with the in-memory log + store.

This is a separate entry point from ``python -m trading_agents`` (the
council CLI). Reflection is OUT OF BAND — it should never share a process
with a live council pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

from trading_agents.llm import LLM
from trading_agents.memory import (
    DecisionEntry,
    InMemoryDecisionLog,
    InMemoryStrategyConfidenceStore,
)
from trading_agents.nodes import reflection_agent_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s — %(message)s")
log = logging.getLogger("agents.reflection.cli")


_DURATION_RE = re.compile(r"^(\d+)(h|d|m)$")


def _parse_since(s: str) -> timedelta:
    match = _DURATION_RE.match(s.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(
            f"--since expects e.g. '24h', '7d', '30m'. Got {s!r}."
        )
    n = int(match.group(1))
    unit = match.group(2)
    return {
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "m": timedelta(minutes=n),
    }[unit]


async def _seed_demo_decisions(log_: InMemoryDecisionLog) -> None:
    """Seed a couple of fake completed trades so the CLI has something to chew.

    Production reflection reads from a real DecisionLog populated by
    ``runtime.run_council``. The CLI seeds three trades for the demo: two
    momentum wins, one momentum loss, all flagged as fill-complete + PnL-known.
    """
    now = datetime.now(timezone.utc)
    for sym, action, pnl, regime, tech, fund, macro in [
        ("NVDA", "BUY", 312.50, "bull", 68.0, 60.0, 62.0),
        ("AAPL", "BUY", 188.00, "bull", 64.0, 58.0, 60.0),
        ("META", "BUY", -245.10, "choppy", 52.0, 49.0, 47.0),
    ]:
        await log_.record(
            DecisionEntry(
                symbol=sym,
                horizon="short",
                triggered_at=now - timedelta(hours=18),
                regime=regime,
                selected_strategy="momentum",
                selector_confidence=0.6,
                selector_rationale="MOCK-SEED: demo decision.",
                final_action=action,
                risk_approved=True,
                technical_score=tech,
                fundamental_score=fund,
                macro_score=macro,
                fill_qty=10,
                fill_avg_price=200.0,
                realized_pnl=pnl,
                raw_state={
                    "proposal": {
                        "bull_case": f"{sym} momentum aligned with regime",
                        "bear_case": "Risk-off would compress fast",
                    }
                },
            )
        )


async def _main(since: timedelta, seed: bool) -> int:
    llm = LLM()
    log.info("LLM mode: %s", "MOCK" if llm.mock else "REAL")

    decision_log = InMemoryDecisionLog()
    confidence_store = InMemoryStrategyConfidenceStore()

    if seed:
        await _seed_demo_decisions(decision_log)
        log.info("Seeded 3 demo decisions in the in-memory log.")

    summary = await reflection_agent_run(
        llm=llm,
        decision_log=decision_log,
        confidence_store=confidence_store,
        since=since,
    )

    print("\n=== REFLECTION SUMMARY ===")
    print(json.dumps(summary, indent=2, default=str))

    print("\n=== CURRENT PRIORS ===")
    for row in await confidence_store.all():
        marker = "*" if row.last_reflection_at else " "
        print(
            f" {marker} {row.strategy_id:<22} confidence={row.confidence:.2f}  "
            f"wins={row.wins} losses={row.losses}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reflection Agent one-shot runner.")
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=timedelta(hours=24),
        help="Window of decisions to review (e.g. 24h, 7d). Default 24h.",
    )
    parser.add_argument(
        "--no-seed",
        dest="seed",
        action="store_false",
        help="Skip the demo-decisions seed. Use when wiring a real log.",
    )
    parser.set_defaults(seed=True)
    args = parser.parse_args()
    return asyncio.run(_main(args.since, args.seed))


if __name__ == "__main__":
    sys.exit(main())
