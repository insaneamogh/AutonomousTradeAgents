"""Feature providers.

Per PLAN.md §5.3: agents do NOT fetch raw data. They consume a pre-computed
feature dict. The provider lives here. In Phase 0 it's synthetic; in Phase 1
the real provider reads from the feature store (Postgres + Redis) populated
by the daily batch jobs.
"""

from trading_agents.features.synthetic import synthetic_features

__all__ = ["synthetic_features"]
