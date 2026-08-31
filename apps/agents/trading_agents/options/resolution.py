"""Deterministic resolution of the Bull/Bear options agents' independent views.

Per ``docs/IMPL_OPTIONS_AGENTS.md`` §4 / ``docs/PLAN_OPTIONS_AGENTS.md`` §2.3:
Bull and Bear each read the identical deterministic pre-pass and form a view
in parallel, neither seeing the other's output (``options/agents.py``). This
module is the ONLY place those two views combine, and it is plain
Python — no LLM call happens here, which is what makes "do the two agents
agree" a fully deterministic, independently unit-testable decision rather
than a third model call that could itself be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AgentView", "Resolution", "resolve"]

# docs/IMPL_OPTIONS_AGENTS.md §4: a gap wider than this means the two agents
# read the same evidence and came away with materially different
# conviction — treated the same as a disagreement, not averaged away.
_MAX_CONVICTION_GAP = 0.4


@dataclass(frozen=True)
class AgentView:
    """One agent's independent read of the pre-pass context.

    ``direction is None`` means the agent is standing down — a valid and
    common answer (IMPL doc §3.2: "If there is no trade, say so"), not a
    failure. ``conviction``/``thesis``/``strategy`` are still populated
    (or left at their defaults) even on a stand-down, purely for audit —
    ``resolve()`` below only ever branches on ``direction`` first.
    """

    role: str
    """"bull" | "bear" — for logging/audit only; ``resolve()`` never
    branches on this, so a mislabeled role cannot change the outcome."""
    direction: str | None
    """"long" | "short" | None (standing down)."""
    conviction: float
    """0..1. Meaningless when ``direction`` is None; still expected to be a
    finite clamped number rather than a sentinel, so arithmetic on it (see
    ``resolve()``) never has to special-case "no view"."""
    thesis: str = ""
    strategy: str | None = None
    degraded: bool = False
    """This agent's LLM call fell back to a neutral/parse-failure default
    rather than a real read of the evidence — carried through so a caller
    that folds this into ``CouncilState.degraded_nodes`` can, without this
    module needing to know what that state shape looks like."""


@dataclass(frozen=True)
class Resolution:
    proceed: bool
    direction: str | None
    conviction: float
    reason: str
    """Named, like a risk veto rule: ``agreed`` | ``agents_disagree`` |
    ``conviction_divergence`` | ``abstained``."""


def resolve(bull: AgentView, bear: AgentView) -> Resolution:
    """Per ``docs/IMPL_OPTIONS_AGENTS.md`` §4 — deterministic, no LLM.

    ``min()``, not the mean. Two agents agreeing WEAKLY is a weak trade;
    averaging would let one enthusiastic agent drag the delta band toward
    the money on the strength of the OTHER agent's lower number. This is a
    deliberate risk control, not an arbitrary tie-break, and it costs
    nothing extra in latency since both agents already ran in parallel
    (docs/PLAN_OPTIONS_AGENTS.md §2.3).
    """
    if bull.direction is None or bear.direction is None:
        return Resolution(False, None, 0.0, "abstained")
    if bull.direction != bear.direction:
        return Resolution(False, None, 0.0, "agents_disagree")
    if abs(bull.conviction - bear.conviction) > _MAX_CONVICTION_GAP:
        return Resolution(False, None, 0.0, "conviction_divergence")
    return Resolution(True, bull.direction, min(bull.conviction, bear.conviction), "agreed")
