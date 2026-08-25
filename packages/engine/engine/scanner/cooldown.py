"""Per-(symbol, rule) debounce — the thing that stops a trigger costing $538/mo.

Trigger conditions are sticky by nature. A name that crosses its 20-DMA at
10:15 is still above its 20-DMA at 10:20, 10:25, 10:30 — the rule keeps
firing, correctly, on every scan. Without a debounce a single genuine cross
would wake the council 78 times in one session.

Two independent layers guard the spend, and both matter:

  1. **This cooldown** — process-local, per (symbol, rule), default 4
     hours. Stops one condition from re-firing while it persists.
  2. **``daily_cron``'s idempotency** — one decision row per (user, symbol,
     UTC day), in Postgres. Survives restarts and covers the case this
     class cannot: the API process redeploying mid-session and forgetting
     everything it had already fired.

Layer 1 alone would be unsafe (a redeploy loop would re-spend); layer 2
alone would be too coarse (it cannot tell a second DMA cross from the same
one still standing). Together the worst case is bounded at one council pass
per symbol per day regardless of how the process behaves.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from engine.scanner.types import ScanSignal

logger = logging.getLogger("engine.scanner.cooldown")

DEFAULT_COOLDOWN_MINUTES = 240


class TriggerCooldown:
    """In-memory last-fired clock keyed by (symbol, rule).

    Deliberately not persisted. The durable guard is the decision-row
    idempotency in ``daily_cron``; making this durable too would mean a
    schema, a migration, and a second source of truth for the same
    question, in exchange for suppressing a handful of redundant scans
    after a redeploy.
    """

    def __init__(self, cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES) -> None:
        self.cooldown = timedelta(minutes=max(0, cooldown_minutes))
        self._last_fired: dict[tuple[str, str], datetime] = {}

    def is_cool(self, symbol: str, rule: str, now: datetime) -> bool:
        """True when this (symbol, rule) is allowed to fire again."""
        last = self._last_fired.get((symbol.upper(), rule))
        if last is None:
            return True
        return (now - last) >= self.cooldown

    def mark(self, symbol: str, rule: str, now: datetime) -> None:
        """Record that this (symbol, rule) just fired."""
        self._last_fired[(symbol.upper(), rule)] = now

    def filter(
        self, signals: list[ScanSignal], now: datetime | None = None
    ) -> tuple[list[ScanSignal], list[ScanSignal]]:
        """Split ``signals`` into (allowed, suppressed) and mark the allowed.

        Marking happens here rather than at the call site so there is no
        path where a signal is acted on without its cooldown being set —
        that omission is silent, and its symptom is a bill.
        """
        at = now or datetime.now(UTC)
        allowed: list[ScanSignal] = []
        suppressed: list[ScanSignal] = []
        for sig in signals:
            if self.is_cool(sig.symbol, sig.trigger_rule, at):
                self.mark(sig.symbol, sig.trigger_rule, at)
                allowed.append(sig)
            else:
                suppressed.append(sig)
        if suppressed:
            logger.debug(
                "scanner: %d signal(s) suppressed by cooldown (%s)",
                len(suppressed),
                ", ".join(f"{s.symbol}:{s.trigger_rule}" for s in suppressed),
            )
        return allowed, suppressed

    def reset(self) -> None:
        """Forget every cooldown. For tests and operator-forced rescans."""
        self._last_fired.clear()
