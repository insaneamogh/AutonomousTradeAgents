"""StrategyConfidenceStore — per-strategy priors maintained by the Reflection loop.

One row per strategy id (seeded from ``STRATEGY_REGISTRY`` at 0.5). The
Reflection Agent updates ``confidence`` by a clamped delta (±0.1 max per
cycle) after grading outcomes. The Selector reads these priors and
prepends them to its prompt as "Strategy priors" — the LLM weighs them
into its pick; the priors do NOT short-circuit the pick.

PLAN.md §5.1 explicitly says Reflection updates Selector priors. Bounded
delta keeps the loop stable under small-N noise (the Phase 4 paper-trading
phase is where we'll have enough data to relax the bound).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from trading_agents.strategies import STRATEGY_REGISTRY

# Hard cap on per-cycle confidence drift. The Reflection Agent's
# confidence_delta is clamped to ±this value before being applied.
MAX_CONFIDENCE_DELTA_PER_CYCLE: float = 0.10

# Hard cap on absolute confidence. Keeps a runaway-prior from forcing a
# strategy lock-in or extinction.
MIN_CONFIDENCE: float = 0.05
MAX_CONFIDENCE: float = 0.95


@dataclass
class StrategyConfidenceRow:
    strategy_id: str
    confidence: float = 0.5
    wins: int = 0
    losses: int = 0
    last_reflection_at: datetime | None = None
    notes: str = ""

    def clamped(self) -> "StrategyConfidenceRow":
        """Return a copy with confidence clipped to the abs bounds."""
        return StrategyConfidenceRow(
            strategy_id=self.strategy_id,
            confidence=max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, self.confidence)),
            wins=self.wins,
            losses=self.losses,
            last_reflection_at=self.last_reflection_at,
            notes=self.notes,
        )


@runtime_checkable
class StrategyConfidenceStore(Protocol):
    """Backend contract — async to allow Postgres later without breaking callers."""

    async def get(self, strategy_id: str) -> StrategyConfidenceRow: ...
    async def all(self) -> list[StrategyConfidenceRow]: ...
    async def apply_delta(
        self,
        strategy_id: str,
        *,
        confidence_delta: float,
        wins: int = 0,
        losses: int = 0,
        notes: str = "",
    ) -> StrategyConfidenceRow: ...


class InMemoryStrategyConfidenceStore:
    """Process-local prior store. Seeded from ``STRATEGY_REGISTRY``.

    Confidence drifts inside ``[MIN_CONFIDENCE, MAX_CONFIDENCE]``. Per-cycle
    delta is clamped to ``MAX_CONFIDENCE_DELTA_PER_CYCLE`` — the Reflection
    Agent is allowed to nudge, not to lock or extinct a strategy.
    """

    def __init__(self) -> None:
        self._rows: dict[str, StrategyConfidenceRow] = {
            sid: StrategyConfidenceRow(strategy_id=sid, confidence=0.5)
            for sid in STRATEGY_REGISTRY
        }

    async def get(self, strategy_id: str) -> StrategyConfidenceRow:
        if strategy_id not in self._rows:
            self._rows[strategy_id] = StrategyConfidenceRow(strategy_id=strategy_id, confidence=0.5)
        return self._rows[strategy_id].clamped()

    async def all(self) -> list[StrategyConfidenceRow]:
        # Return clamped copies so callers can't mutate internal rows.
        return [row.clamped() for row in self._rows.values()]

    async def apply_delta(
        self,
        strategy_id: str,
        *,
        confidence_delta: float,
        wins: int = 0,
        losses: int = 0,
        notes: str = "",
    ) -> StrategyConfidenceRow:
        if strategy_id not in self._rows:
            self._rows[strategy_id] = StrategyConfidenceRow(strategy_id=strategy_id, confidence=0.5)

        row = self._rows[strategy_id]

        # Clamp the delta first, then apply, then clamp the absolute value.
        bounded_delta = max(
            -MAX_CONFIDENCE_DELTA_PER_CYCLE,
            min(MAX_CONFIDENCE_DELTA_PER_CYCLE, confidence_delta),
        )
        row.confidence = max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, row.confidence + bounded_delta))
        row.wins += wins
        row.losses += losses
        row.last_reflection_at = datetime.now(timezone.utc)
        if notes:
            row.notes = notes
        return row.clamped()
