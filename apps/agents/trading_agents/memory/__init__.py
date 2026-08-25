"""Council memory — decision log + strategy confidence store.

Two protocols + their in-memory implementations:

  - ``DecisionLog`` records every council pass (one row per ``run_council``
    call). The Reflection Agent reads from this — without a log there's
    nothing to reflect on.

  - ``StrategyConfidenceStore`` holds the per-strategy priors that the
    Selector reads on each pass and the Reflection Agent updates EOD.

Both ship as ``Protocol`` + ``InMemory*`` impls. Postgres backings sit in
``trading_agents.memory.postgres`` and turn on via ``USE_POSTGRES=1``.

Architectural note: this module lives in the AGENTS package, not the API
package. The council is the producer; the API will be a consumer of the
same data later. Keeping it agent-side now avoids pulling FastAPI into
``run_council``'s import graph.
"""

from __future__ import annotations

from engine.env import env_flag
from trading_agents.memory.decision_log import (
    DecisionEntry,
    DecisionLog,
    InMemoryDecisionLog,
)
from trading_agents.memory.strategy_confidence import (
    InMemoryStrategyConfidenceStore,
    StrategyConfidenceRow,
    StrategyConfidenceStore,
)

__all__ = [
    "DecisionEntry",
    "DecisionLog",
    "InMemoryDecisionLog",
    "InMemoryStrategyConfidenceStore",
    "StrategyConfidenceRow",
    "StrategyConfidenceStore",
    "get_confidence_store",
    "get_decision_log",
    "reset_memory_stores_for_tests",
]


_decision_log: DecisionLog | None = None
_confidence_store: StrategyConfidenceStore | None = None


def get_decision_log() -> DecisionLog:
    """Singleton ``DecisionLog`` for this process.

    ``USE_POSTGRES=1`` → ``PostgresDecisionLog`` wired against
    migration 0001 + 0003. Otherwise ``InMemoryDecisionLog``.
    """
    global _decision_log
    if _decision_log is None:
        if env_flag("USE_POSTGRES"):
            from trading_agents.memory.postgres import PostgresDecisionLog

            _decision_log = PostgresDecisionLog()
        else:
            _decision_log = InMemoryDecisionLog()
    return _decision_log


def get_confidence_store() -> StrategyConfidenceStore:
    """Singleton prior store. Same env switch as ``get_decision_log``."""
    global _confidence_store
    if _confidence_store is None:
        if env_flag("USE_POSTGRES"):
            from trading_agents.memory.postgres import PostgresStrategyConfidenceStore

            _confidence_store = PostgresStrategyConfidenceStore()
        else:
            _confidence_store = InMemoryStrategyConfidenceStore()
    return _confidence_store


def reset_memory_stores_for_tests() -> None:
    global _decision_log, _confidence_store
    _decision_log = None
    _confidence_store = None
