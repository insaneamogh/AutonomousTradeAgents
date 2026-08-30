"""Strategy-fit node — deterministic, LLM-free, and FIRST in the graph.

This is what replaced the Selector LLM node. It reads the feature dict,
asks every strategy to score its own preconditions
(``trading_agents.strategies.fit``), applies the Reflection loop's priors
as a bounded multiplier, and picks the winner in Python.

Two consequences, in order of how much they matter:

  1. **HOLD is free.** The node runs BEFORE the Router, so a symbol whose
     setup fits nothing costs zero LLM calls instead of five. This is the
     whole point: on a watchlist sweep, most symbols on most days are not
     setups, and the old graph paid full price to be told so.

  2. **The pick is auditable.** ``selector_rationale`` is now a named
     reason assembled from the component checks that carried the decision
     (``momentum_short:trailing_3m_return+risk_adjusted+trend_regime_aligned``)
     rather than a sentence a model wrote. Same string, strictly more
     information, and a test can assert on it.

**Why the Selector LLM node is gone rather than demoted to an explainer.**
A demoted node would spend a Haiku call to produce prose describing numbers
that are already in the audit row in structured form — decorative by
construction. Worse, it would reintroduce a failure mode on a path that
currently cannot fail: every LLM node in this council can degrade on a
parse error, and a degraded *explainer* would mark the whole run degraded
(excluding it from calibration) over cosmetics. The UI renders
``strategy_fit.components`` directly; that is a better explanation than a
paraphrase, and it costs nothing.

State contract (unchanged keys keep the DB columns and the Reflection loop
working):
    selected_strategy    strategy id, or None → HOLD
    selector_confidence  the prior-adjusted fit score, 0..1
    selector_rationale   the named reason
    selected_direction   "long" | "short"   (new)
    strategy_fit         full winner + ranking, for the audit row + UI (new)
    instrument           "option", additive — ONLY when ALLOW_OPTIONS AND
                         state["instrument_preference"] == "option" are
                         BOTH set and a strategy won. Absent otherwise,
                         which is every existing behavior, unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.env import env_flag
from trading_agents.state import CouncilState
from trading_agents.strategies import best_strategy
from trading_agents.strategies.fit import _has_usable_features

logger = logging.getLogger("agents.node.strategy_fit")

MAX_RANKED_PERSISTED = 6
"""Ranked alternatives kept on the decision row. Enough to show "why this
one and not that one" in the UI without storing the full cross product."""


async def strategy_fit_node(state: CouncilState) -> CouncilState:
    """Pick the strategy + direction deterministically. No LLM, no I/O.

    Async only to match every other node's signature — the graph awaits
    them uniformly and a sync exception here would be the odd one out.
    """
    features = state.get("context") or {}
    priors = state.get("strategy_priors") or {}
    allow_shorts = env_flag("ALLOW_SHORTS")

    winner, ranked = best_strategy(
        features, priors=priors, allow_shorts=allow_shorts
    )

    # Re-derived here (not threaded through `best_strategy`'s return value)
    # purely to explain a None winner — see that function's docstring. This
    # never changes the decision, only the rationale text and the audit
    # block below it.
    usable_features, unusable_reason = _has_usable_features(features)

    fit_block: dict[str, Any] = {
        "allow_shorts": allow_shorts,
        "winner": winner.as_dict() if winner else None,
        "ranked": [r.as_dict() for r in ranked[:MAX_RANKED_PERSISTED]],
        "priors_applied": dict(priors),
        "usable_features": usable_features,
    }
    if not usable_features:
        fit_block["unusable_reason"] = unusable_reason

    if winner is None:
        top = ranked[0] if ranked else None
        if not usable_features:
            # Distinct from the "genuinely marginal" branch below: `top`
            # (if any) may well show a score at or above MIN_FIT_TO_TRADE
            # here — that's the exact leak this branch exists to name
            # correctly rather than let read as "best was X at 0.60" while
            # returning HOLD, which looks like a bug rather than a gate.
            rationale = (
                f"Feature data too thin to trade ({unusable_reason}) — "
                "holding without spending an LLM call."
            )
        elif top:
            rationale = (
                f"No strategy clears the fit floor — best was {top.strategy_id} "
                f"({top.direction}) at {top.score:.2f}. Holding without spending an LLM call."
            )
        else:
            rationale = "No strategy could be scored (no usable features). Holding."
        logger.info(
            "strategy_fit: %s → HOLD before any LLM call (usable_features=%s best=%s %.3f)",
            state.get("symbol"),
            usable_features,
            top.strategy_id if top else "none",
            top.score if top else 0.0,
        )
        return {
            **state,
            "selected_strategy": None,
            "selected_direction": None,
            "selector_confidence": 0.0,
            "selector_rationale": rationale,
            "strategy_fit": fit_block,
            "proposal": None,
            "final_action": "HOLD",
        }

    logger.info(
        "strategy_fit: %s → %s %s (fit=%.3f prior=%.2f score=%.3f) %s",
        state.get("symbol"),
        winner.strategy_id,
        winner.direction,
        winner.fit,
        winner.prior,
        winner.score,
        winner.reason,
    )
    result: CouncilState = {
        **state,
        "selected_strategy": winner.strategy_id,
        "selected_direction": winner.direction,
        "selector_confidence": winner.score,
        "selector_rationale": winner.reason,
        "strategy_fit": fit_block,
    }

    # Options instrument gate — additive, and the ONLY new branch in this
    # node. Both the master ALLOW_OPTIONS env switch (mirrors ALLOW_SHORTS
    # above — same env_flag helper, same fail-closed default) AND a
    # per-run instrument preference must be set, and a strategy must have
    # actually won (we are past the `winner is None` branch, so it has).
    # When either condition is absent, `result` is exactly what this node
    # has always returned — nothing above this comment changed.
    if env_flag("ALLOW_OPTIONS") and state.get("instrument_preference") == "option":
        result["instrument"] = "option"

    return result
