"""Council progress events — the theater feed.

``run_graph(..., progress_cb=...)`` emits one ``ProgressEvent`` per node
transition. Summaries are extracted deterministically from ``CouncilState``
(no LLM in this path) and kept tiny — a score, a confidence, a one-line
thesis — exactly what the mobile theater screen renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

NodeName = Literal[
    "router",
    "technical",
    "fundamental",
    "macro",
    "selector",
    "drafter",
    "risk_officer",
]

NodeStatus = Literal["started", "completed", "skipped"]

# Display order for clients — the canonical run sequence.
NODE_ORDER: tuple[NodeName, ...] = (
    "router",
    "technical",
    "fundamental",
    "macro",
    "selector",
    "drafter",
    "risk_officer",
)

_THESIS_MAX = 140


@dataclass
class ProgressEvent:
    """One node transition. ``seq`` is assigned by the emitter."""

    seq: int
    node: NodeName
    status: NodeStatus
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Optional deterministic summary, shape depends on node:
    #   analysts     → {score, confidence, thesis}
    #   router       → {regime, analystSubset, thesis}
    #   selector     → {strategy, confidence, thesis}
    #   drafter      → {side, qty, notional, thesis}
    #   risk_officer → {approved, vetoRule, thesis}
    summary: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "node": self.node,
            "status": self.status,
            "at": self.at.isoformat(),
            "summary": self.summary,
        }


ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


def _clip(text: object) -> str:
    s = str(text or "").strip()
    return s if len(s) <= _THESIS_MAX else s[: _THESIS_MAX - 1] + "…"


def summarize_node(node: NodeName, state: dict[str, Any]) -> dict[str, Any] | None:
    """Deterministic per-node summary from the (post-node) council state."""
    if node == "router":
        return {
            "regime": state.get("regime"),
            "analystSubset": list(state.get("analyst_subset") or []),
            "thesis": _clip(state.get("router_rationale")),
        }
    if node in ("technical", "fundamental", "macro"):
        out = state.get(node) or {}
        return {
            "score": out.get("score"),
            "confidence": out.get("confidence"),
            "thesis": _clip(out.get("thesis")),
        }
    if node == "selector":
        return {
            "strategy": state.get("selected_strategy"),
            "confidence": state.get("selector_confidence"),
            "thesis": _clip(state.get("selector_rationale")),
        }
    if node == "drafter":
        p = state.get("proposal") or {}
        return {
            "side": p.get("side"),
            "qty": p.get("qty"),
            "notional": p.get("estimated_notional"),
            "thesis": _clip(p.get("rationale")),
        }
    if node == "risk_officer":
        return {
            "approved": bool(state.get("risk_approved", False)),
            "vetoRule": state.get("risk_veto_rule"),
            "thesis": _clip(state.get("risk_reason")),
        }
    return None
