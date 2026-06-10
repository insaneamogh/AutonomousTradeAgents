"""Reflection Agent — closes the council loop. Sonnet-tier.

Runs OUT OF BAND (EOD / EOW). Unlike the council nodes this is NOT a
``CouncilState`` transformer — it has its own coroutine signature because
it operates on stored decisions, not on a live pass. PLAN.md §5.1 is
explicit that Reflection updates Selector priors; this is the only
write-path into ``StrategyConfidenceStore``.

Architectural notes:
  - Reflection NEVER writes to broker / risk / executor paths. Its outputs
    are confidence deltas + a ``reviewed_at`` timestamp.
  - Confidence delta is double-clamped: the prompt asks for ±0.10; the
    store re-clamps in ``apply_delta``. Don't trust the model to obey.
  - Small-N safety: with fewer than 3 completed trades on a strategy we
    still process it — the prompt rules say "small N → small delta", and
    the store clamps regardless.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from trading_agents.llm import LLM, Model
from trading_agents.memory import (
    DecisionEntry,
    DecisionLog,
    StrategyConfidenceStore,
)
from trading_agents.prompts import REFLECTION

logger = logging.getLogger("agents.node.reflection")


async def reflection_agent_run(
    *,
    llm: LLM,
    decision_log: DecisionLog,
    confidence_store: StrategyConfidenceStore,
    since: timedelta = timedelta(hours=24),
    limit: int = 200,
) -> dict[str, Any]:
    """Reflect on the last ``since`` window. One LLM call per strategy id.

    Returns a summary dict suitable for logging / CLI output:

        {
          "reviewed": <int total decisions touched>,
          "per_strategy": {
              "<id>": {
                  "wins": int, "losses": int,
                  "confidence_before": float, "confidence_after": float,
                  "delta_applied": float, "lessons": [str],
              },
              ...
          }
        }
    """
    pending = await decision_log.list_pending_reflection(since=since, limit=limit)
    if not pending:
        logger.info("reflection: no pending decisions in last %s", since)
        return {"reviewed": 0, "per_strategy": {}}

    # Group by strategy_id; skip rows where strategy is None (the Selector HOLD'd).
    by_strategy: dict[str, list[DecisionEntry]] = defaultdict(list)
    for d in pending:
        if d.selected_strategy:
            by_strategy[d.selected_strategy].append(d)

    summary: dict[str, Any] = {"reviewed": 0, "per_strategy": {}}

    for strategy_id, trades in by_strategy.items():
        prior = await confidence_store.get(strategy_id)
        user_prompt = _build_user_prompt(strategy_id, prior.confidence, trades)

        resp = await llm.complete(
            system=REFLECTION,
            user=user_prompt,
            model=Model.SONNET,
            max_tokens=900,
        )
        try:
            data = LLM.parse_json(resp.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflection: parse failed for %s — skipping (%s)",
                strategy_id, exc,
            )
            # Don't mark these decisions reviewed; we'll retry next cycle.
            continue

        delta = float(data.get("confidence_delta", 0.0) or 0.0)
        wins = int(data.get("wins", 0) or 0)
        losses = int(data.get("losses", 0) or 0)
        lessons = list(data.get("lessons") or [])
        notes = str(data.get("notes", "")).strip()

        # Apply (store clamps the delta + abs bounds).
        updated = await confidence_store.apply_delta(
            strategy_id,
            confidence_delta=delta,
            wins=wins,
            losses=losses,
            notes=notes,
        )

        # Mark each decision reviewed AFTER the prior is updated so a mid-
        # batch crash doesn't leave decisions reviewed-without-applied.
        for trade in trades:
            await decision_log.mark_reviewed(trade.id)

        summary["per_strategy"][strategy_id] = {
            "wins": wins,
            "losses": losses,
            "confidence_before": prior.confidence,
            "confidence_after": updated.confidence,
            "delta_applied": updated.confidence - prior.confidence,
            "lessons": lessons,
            "notes": notes,
        }
        summary["reviewed"] += len(trades)

    return summary


def _build_user_prompt(
    strategy_id: str,
    prior_confidence: float,
    trades: list[DecisionEntry],
) -> str:
    lines: list[str] = [
        f"Strategy id: {strategy_id}",
        f"Current prior confidence: {prior_confidence:.2f}",
        f"Completed trades in window: {len(trades)}",
        "",
        "Trades:",
    ]
    for t in trades:
        # Pull bull/bear from raw_state.proposal if present; fall back gracefully.
        raw = t.raw_state or {}
        proposal = raw.get("proposal") if isinstance(raw, dict) else None
        bull = (proposal or {}).get("bull_case", "") if proposal else ""
        bear = (proposal or {}).get("bear_case", "") if proposal else ""
        lines.append(
            f"  - {t.symbol}  regime={t.regime}  "
            f"tech={t.technical_score} fund={t.fundamental_score} macro={t.macro_score}  "
            f"action={t.final_action} qty={t.fill_qty} avg=${t.fill_avg_price} pnl=${t.realized_pnl}"
        )
        if bull:
            lines.append(f"      bull: {bull[:160]}")
        if bear:
            lines.append(f"      bear: {bear[:160]}")
    return "\n".join(lines) + "\n"
