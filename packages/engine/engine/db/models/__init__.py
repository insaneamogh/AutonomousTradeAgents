"""SQLAlchemy models, grouped by what they are FOR.

The schema is the system's memory, so it is split along the same seam as
the architecture rather than piled into one file:

    accounts   Users and the credentials/config hanging off them.
    council    What the agents proposed, and what it cost.
    trading    Orders, fills, and the risk state that gates them.

Importing this package registers every table on ``Base.metadata`` — which
is what Alembic autogenerate walks — so ``from engine.db.models import X``
keeps working for any model regardless of which module now defines it.

NOT modelled yet (deliberate):
    strategies / strategy_versions   → when hand-coded references are versioned
    feature_store                    → Phase 2
    psychology_reports               → Phase 5
"""

from engine.db.models.accounts import (
    BrokerConnection,
    DeviceToken,
    MagicLinkToken,
    User,
    UserSession,
    UserWatchlistItem,
)
from engine.db.models.council import (
    AgentDecision,
    DecisionReview,
    GhostOutcome,
    LlmCall,
    StrategyConfidence,
)
from engine.db.models.trading import (
    CircuitBreakerState,
    Order,
    OrderFill,
    PdtLedger,
    PositionsSnapshot,
)

__all__ = [
    "AgentDecision",
    "BrokerConnection",
    "CircuitBreakerState",
    "DecisionReview",
    "DeviceToken",
    "GhostOutcome",
    "LlmCall",
    "MagicLinkToken",
    "Order",
    "OrderFill",
    "PdtLedger",
    "PositionsSnapshot",
    "StrategyConfidence",
    "User",
    "UserSession",
    "UserWatchlistItem",
]
