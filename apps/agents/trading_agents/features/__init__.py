"""Feature providers.

Per PLAN.md §5.3: agents do NOT fetch raw data. They consume a pre-computed
feature dict. Real computation lives engine-side (``engine.features``);
this module only resolves WHICH provider a run uses:

  - Alpaca data keys present → ``RealFeatureProvider`` (IEX bars → real
    technicals, FRED macro, fundamentals only when a real source is wired).
  - No keys → synthetic (dev/CI), unless ``AGENTS_REQUIRE_REAL_DATA=1``
    turns that fallback into a hard failure — production must never
    silently trade on hash-generated features again.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from trading_agents.features.synthetic import synthetic_features

logger = logging.getLogger("agents.features")

FeatureProvider = Callable[..., Any]


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name)
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def resolve_feature_provider(
    *,
    equity_resolver: Callable[[], Awaitable[float | None]] | None = None,
) -> FeatureProvider:
    """The provider production entry points (daily cron, /agent/run) use."""
    from engine.features import feature_provider_from_env

    provider = feature_provider_from_env(equity_resolver=equity_resolver)
    if provider is not None:
        logger.info("features: REAL provider active (Alpaca bars%s)",
                    " + FRED" if provider.fred_api_key else ", no FRED key")
        return provider

    if _env_truthy("AGENTS_REQUIRE_REAL_DATA"):
        raise RuntimeError(
            "AGENTS_REQUIRE_REAL_DATA=1 but ALPACA_API_KEY/ALPACA_SECRET_KEY "
            "are not set — refusing to run the council on synthetic features."
        )
    logger.warning(
        "features: SYNTHETIC provider active (no Alpaca data keys). "
        "Dev/CI only — decisions mean nothing against real markets."
    )
    return synthetic_features


__all__ = ["FeatureProvider", "resolve_feature_provider", "synthetic_features"]
