"""Deterministic post-processing for LLM node output.

The architectural rule is "agents propose, deterministic code disposes".
Prompts *ask* analysts for a 0-100 score and a 0-1 confidence, but a
prompt is advice, not an invariant — a model that returns ``score: 900``
(whether it hallucinated or was steered there by an injected ticker)
would sail into ``SpecialistScore`` and drag the council average past the
``min_specialist_avg_score`` veto floor on its own. A model that returns
``risk_level: "high"`` used to raise ValueError and kill the whole run.

So every number an LLM hands back crosses one of these functions before
it touches state. They never raise: an unusable value becomes the neutral
default, because a degraded-but-bounded council pass is recoverable and a
crashed one is not.

Clamping belongs HERE and not in the prompt — the prompt already asks
nicely, and the enforcement has to hold even when the model ignores it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("agents.node.guards")

# Analyst score scale — mirrors the prompts and the risk engine's
# ``min_specialist_avg_score`` floor.
SCORE_MIN = 0.0
SCORE_MAX = 100.0
SCORE_NEUTRAL = 50.0

# Confidence is a probability-like weight.
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0

# Drafter's 1-5 ordinals (risk_level, conviction_level).
LEVEL_MIN = 1
LEVEL_MAX = 5
LEVEL_NEUTRAL = 3


def _as_float(value: object, *, default: float, field: str) -> float:
    """``float(value)`` that logs and falls back instead of raising."""
    if isinstance(value, bool):
        # bool is an int subclass; a True score is a bug, not a 1.0.
        logger.warning("%s: got bool %r — using default %s", field, value, default)
        return default
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("%s: unparseable value %r — using default %s", field, value, default)
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        # NaN/inf survive float() but poison every average downstream.
        logger.warning("%s: non-finite value %r — using default %s", field, value, default)
        return default
    return parsed


def clamp_score(value: object, *, field: str = "score") -> float:
    """Coerce an analyst score into [0, 100], defaulting to 50 (neutral)."""
    parsed = _as_float(value, default=SCORE_NEUTRAL, field=field)
    clamped = max(SCORE_MIN, min(SCORE_MAX, parsed))
    if clamped != parsed:
        logger.warning("%s: %s out of range — clamped to %s", field, parsed, clamped)
    return clamped


def clamp_confidence(value: object, *, field: str = "confidence") -> float:
    """Coerce a confidence into [0, 1], defaulting to 0.0 (no weight)."""
    parsed = _as_float(value, default=CONFIDENCE_MIN, field=field)
    clamped = max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, parsed))
    if clamped != parsed:
        logger.warning("%s: %s out of range — clamped to %s", field, parsed, clamped)
    return clamped


def clamp_level(value: object, *, field: str, default: int = LEVEL_NEUTRAL) -> int:
    """Coerce a 1-5 ordinal, defaulting to 3 (middle of the scale).

    Accepts the float and numeric-string forms models actually emit
    (``3.5`` → 3, ``"4"`` → 4) and refuses the word forms (``"high"`` →
    default) rather than raising ValueError mid-run.
    """
    parsed = _as_float(value, default=float(default), field=field)
    clamped = max(LEVEL_MIN, min(LEVEL_MAX, int(parsed)))
    if clamped != parsed:
        logger.warning("%s: %r coerced to %s", field, value, clamped)
    return clamped
