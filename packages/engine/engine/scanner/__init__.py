"""Continuous deterministic scanner — the cheap gate in front of the council.

The council is six LLM nodes and costs ~$0.066 per symbol per pass. Running
it on a fixed clock over the whole watchlist is either too coarse (twice a
day, missing everything in between) or ruinous (a 15-minute sweep of 15
symbols is ~$538/month). Neither is a real answer, and the expensive one is
also pointless: every technical the council reads is computed from DAILY
bars, which do not change intraday.

So the work splits in two:

  - **Cheap, continuous, deterministic (this package).** Poll the intraday
    tape every few minutes; compare live price against levels derived from
    settled daily bars; emit named ``ScanSignal``s. One batched market-data
    request per pass, zero LLM tokens.
  - **Expensive, occasional, judgement (the council).** Runs only for the
    symbols that tripped a trigger, and receives the trigger context so the
    analysts know why they were woken.

This is the same principle as the risk engine, applied to spend instead of
safety: deterministic Python decides *whether* to think, and the named rule
identifier makes the decision auditable after the fact.

Public surface::

    from engine.scanner import (
        Scanner, ScannerConfig, ScanResult, ScanSignal,
        SymbolSnapshot, TriggerRule, TriggerCooldown,
        build_snapshot, evaluate_triggers, scanner_from_env,
    )
"""

from engine.scanner.cooldown import DEFAULT_COOLDOWN_MINUTES, TriggerCooldown
from engine.scanner.engine import (
    DAILY_LOOKBACK_DAYS,
    RS_WINDOW,
    Scanner,
    build_snapshot,
)
from engine.scanner.select import scanner_from_env
from engine.scanner.triggers import TRIGGERS, evaluate_triggers
from engine.scanner.types import (
    Direction,
    ScannerConfig,
    ScanResult,
    ScanSignal,
    SymbolSnapshot,
    TriggerRule,
)

__all__ = [
    "DAILY_LOOKBACK_DAYS",
    "DEFAULT_COOLDOWN_MINUTES",
    "RS_WINDOW",
    "TRIGGERS",
    "Direction",
    "ScanResult",
    "ScanSignal",
    "Scanner",
    "ScannerConfig",
    "SymbolSnapshot",
    "TriggerCooldown",
    "TriggerRule",
    "build_snapshot",
    "evaluate_triggers",
    "scanner_from_env",
]
