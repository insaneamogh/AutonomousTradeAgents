"""Reconciler (Phase 1).

Periodic asyncio task: polls the broker → writes a ``positions_snapshot``
row → evaluates the drawdown circuit breaker → halts on threshold breach.

Public surface:
    from engine.reconciler import (
        Reconciler, ReconcilerConfig, ReconcilerTickResult,
        BrokerPoller, MockBrokerPoller, AlpacaBrokerPoller, RawAccountState,
        write_snapshot, evaluate_breaker, BreakerTransition,
    )

Architecture rule (PLAN.md §6.4 + §12): the reconciler is the only thing
that flips ``circuit_breaker_state``. Halt persists until the user
explicitly acknowledges via the API — no auto-unhalt on a new trading day.
"""

from engine.reconciler.breaker import BreakerTransition, evaluate_breaker
from engine.reconciler.poller import (
    AlpacaBrokerPoller,
    BrokerPoller,
    MockBrokerPoller,
    RawAccountState,
)
from engine.reconciler.reconciler import (
    Reconciler,
    ReconcilerConfig,
    ReconcilerTickResult,
)
from engine.reconciler.snapshot import write_snapshot

__all__ = [
    "AlpacaBrokerPoller",
    "BreakerTransition",
    "BrokerPoller",
    "MockBrokerPoller",
    "RawAccountState",
    "Reconciler",
    "ReconcilerConfig",
    "ReconcilerTickResult",
    "evaluate_breaker",
    "write_snapshot",
]
