"""Per-strategy performance aggregator.

Reads:
  - ``DecisionLog.all_decisions()`` (or filters by user_id once we have
    a per-user view; in-memory is single-user)
  - ``StrategyConfidenceStore.all()``  for the priors

Aggregates a rolling 30-day (configurable) window per ``selected_strategy``:
  - decisions_in_window: total count
  - wins / losses: completed trades only (``realized_pnl IS NOT NULL``)
  - realized_pnl: sum
  - avg_winner_pct / avg_loser_pct: % vs notional, only on completed
  - last_decision_at: most recent triggered_at
  - last_reflection_at: from the prior row

Returns one row per ``STRATEGY_REGISTRY`` id even when the window is
empty for that strategy — gives the mobile UI a stable list to render
on Phase 4 day 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trading_agents.memory import get_confidence_store, get_decision_log
from trading_agents.strategies import STRATEGY_REGISTRY

from app.schemas.strategies import StrategiesPerformanceResponse, StrategyPerformanceDto

logger = logging.getLogger("api.strategies_perf")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Aggregate:
    decisions_in_window: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    winner_pct_sum: float = 0.0
    loser_pct_sum: float = 0.0
    last_decision_at: datetime | None = None


async def build_strategies_performance(
    *,
    window_days: int = 30,
) -> StrategiesPerformanceResponse:
    decision_log = get_decision_log()
    confidence_store = get_confidence_store()

    cutoff = _now() - timedelta(days=window_days)

    try:
        decisions = await decision_log.all_decisions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategies_perf: decision-log read failed — %s", exc)
        decisions = []

    try:
        priors = {p.strategy_id: p for p in await confidence_store.all()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategies_perf: confidence-store read failed — %s", exc)
        priors = {}

    agg: dict[str, _Aggregate] = {sid: _Aggregate() for sid in STRATEGY_REGISTRY}

    for d in decisions:
        if d.selected_strategy is None:
            continue
        if d.triggered_at < cutoff:
            continue

        bucket = agg.setdefault(d.selected_strategy, _Aggregate())
        bucket.decisions_in_window += 1
        if bucket.last_decision_at is None or d.triggered_at > bucket.last_decision_at:
            bucket.last_decision_at = d.triggered_at

        # Only completed trades count toward wins/losses + PnL.
        if d.realized_pnl is None:
            continue
        # Notional approximation — fill_qty * fill_avg_price. Fall back to
        # 1 to keep the divisor safe.
        notional = max(
            (d.fill_qty or 0) * (d.fill_avg_price or 0.0),
            1.0,
        )
        pct = (d.realized_pnl / notional) * 100.0
        bucket.realized_pnl += d.realized_pnl
        if d.realized_pnl >= 0:
            bucket.wins += 1
            bucket.winner_pct_sum += pct
        else:
            bucket.losses += 1
            bucket.loser_pct_sum += pct

    rows: list[StrategyPerformanceDto] = []
    for sid, meta in STRATEGY_REGISTRY.items():
        bucket = agg.get(sid, _Aggregate())
        prior = priors.get(sid)
        rows.append(
            StrategyPerformanceDto(
                strategy_id=sid,
                display_name=meta.display,
                confidence=float(prior.confidence) if prior is not None else 0.5,
                decisions_in_window=bucket.decisions_in_window,
                wins=bucket.wins,
                losses=bucket.losses,
                realized_pnl=bucket.realized_pnl,
                avg_winner_pct=(bucket.winner_pct_sum / bucket.wins) if bucket.wins else None,
                avg_loser_pct=(bucket.loser_pct_sum / bucket.losses) if bucket.losses else None,
                last_decision_at=bucket.last_decision_at,
                last_reflection_at=prior.last_reflection_at if prior is not None else None,
            )
        )

    rows.sort(key=lambda r: r.confidence, reverse=True)

    return StrategiesPerformanceResponse(
        window_days=window_days,
        strategies=rows,
        generated_at=_now(),
    )
