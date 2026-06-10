"""LangGraph wiring + a plain-async fallback.

LangGraph is the right substrate for the council when graphs branch
(parallel analyst fan-out, conditional re-runs, reflection loops back to
the Strategy Selector). For the Phase 0 linear path we run plain asyncio
when ``langgraph`` isn't installed — same node functions, same state
shape, no behavior difference.

Both code paths invoke the exact same node coroutines. Adding a new node
means: write the node in ``trading_agents.nodes``, then add it in BOTH
branches below.

Phase 2 update: Strategy Selector is now split into ``selector_node`` (Haiku,
picks a strategy id or HOLDs) and ``drafter_node`` (Sonnet, builds the
proposal narrative + delegates sizing). Selector HOLD short-circuits the
graph — Drafter is skipped and final_action="HOLD".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from trading_agents.llm import LLM
from trading_agents.progress import (
    NodeName,
    ProgressCallback,
    ProgressEvent,
    summarize_node,
)
from trading_agents.nodes import (
    drafter_node,
    fundamental_analyst_node,
    macro_analyst_node,
    risk_officer_node,
    router_node,
    selector_node,
    technical_analyst_node,
)
from engine.risk import RiskCaps
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.graph")

try:
    from langgraph.graph import END, StateGraph  # type: ignore[import-untyped]

    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False
    logger.info("langgraph not installed — running with plain asyncio fallback")


# ─────────────────────────────────────────────────────────────────────
# Plain-asyncio fallback
# ─────────────────────────────────────────────────────────────────────


async def _run_linear(
    state: CouncilState,
    llm: LLM,
    risk_caps: RiskCaps,
    progress_cb: ProgressCallback | None = None,
    pacing_seconds: float = 0.0,
) -> CouncilState:
    """Execute the linear council pipeline without LangGraph.

    ``progress_cb`` (theater feed) gets started/completed/skipped events per
    node. ``pacing_seconds`` inserts an artificial pause after each node —
    used only in MOCK mode so the theater doesn't finish in one frame.
    """
    seq = 0

    async def _emit(node: NodeName, status: str, with_summary: bool = False) -> None:
        nonlocal seq
        if progress_cb is None:
            return
        seq += 1
        summary = summarize_node(node, dict(state)) if with_summary else None
        await progress_cb(
            ProgressEvent(seq=seq, node=node, status=status, summary=summary)  # type: ignore[arg-type]
        )

    async def _pace() -> None:
        if progress_cb is not None and pacing_seconds > 0:
            await asyncio.sleep(pacing_seconds)

    await _emit("router", "started")
    state = await router_node(state, llm)
    await _pace()
    await _emit("router", "completed", with_summary=True)

    subset = state.get("analyst_subset", ["technical"])
    for analyst, node_fn in (
        ("technical", technical_analyst_node),
        ("fundamental", fundamental_analyst_node),
        ("macro", macro_analyst_node),
    ):
        if analyst in subset:
            await _emit(analyst, "started")  # type: ignore[arg-type]
            state = await node_fn(state, llm)
            await _pace()
            await _emit(analyst, "completed", with_summary=True)  # type: ignore[arg-type]
        else:
            await _emit(analyst, "skipped")  # type: ignore[arg-type]

    await _emit("selector", "started")
    state = await selector_node(state, llm)
    await _pace()
    await _emit("selector", "completed", with_summary=True)
    # Selector HOLD short-circuits the council — final_action is already set
    # and there is no proposal to draft or risk-check.
    if state.get("selected_strategy") is None:
        await _emit("drafter", "skipped")
        await _emit("risk_officer", "skipped")
        return state

    await _emit("drafter", "started")
    state = await drafter_node(state, llm)
    await _pace()
    await _emit("drafter", "completed", with_summary=True)

    await _emit("risk_officer", "started")
    state = await risk_officer_node(state, risk_caps)
    if state.get("risk_approved"):
        state["final_action"] = state.get("proposal", {}).get("side", "HOLD")  # type: ignore[union-attr]
    await _pace()
    await _emit("risk_officer", "completed", with_summary=True)
    return state


# ─────────────────────────────────────────────────────────────────────
# LangGraph wiring (used when the lib is installed)
# ─────────────────────────────────────────────────────────────────────


def _build_langgraph(llm: LLM, risk_caps: RiskCaps) -> Callable[[CouncilState], Awaitable[CouncilState]]:
    if not _HAS_LANGGRAPH:
        raise RuntimeError("langgraph not installed")

    g: StateGraph = StateGraph(CouncilState)

    async def _router(state: CouncilState) -> CouncilState:
        return await router_node(state, llm)

    async def _tech(state: CouncilState) -> CouncilState:
        return await technical_analyst_node(state, llm)

    async def _fund(state: CouncilState) -> CouncilState:
        return await fundamental_analyst_node(state, llm)

    async def _macro(state: CouncilState) -> CouncilState:
        return await macro_analyst_node(state, llm)

    async def _selector(state: CouncilState) -> CouncilState:
        return await selector_node(state, llm)

    async def _drafter(state: CouncilState) -> CouncilState:
        return await drafter_node(state, llm)

    async def _risk(state: CouncilState) -> CouncilState:
        state = await risk_officer_node(state, risk_caps)
        if state.get("risk_approved"):
            state["final_action"] = state.get("proposal", {}).get("side", "HOLD")  # type: ignore[union-attr]
        return state

    g.add_node("router", _router)
    g.add_node("technical", _tech)
    g.add_node("fundamental", _fund)
    g.add_node("macro", _macro)
    g.add_node("selector", _selector)
    g.add_node("drafter", _drafter)
    g.add_node("risk_officer", _risk)

    g.set_entry_point("router")

    # Conditional fan-in: route through technical → fundamental → macro →
    # selector → (drafter | END) → risk_officer, skipping any analysts that
    # aren't in the Router's analyst_subset. Phase 2 swaps the serial analyst
    # path for parallel fan-out via a join node.

    def _route_after_router(state: CouncilState) -> str:
        subset = state.get("analyst_subset", ["technical"])
        if "technical" in subset:
            return "technical"
        if "fundamental" in subset:
            return "fundamental"
        if "macro" in subset:
            return "macro"
        return "selector"

    g.add_conditional_edges("router", _route_after_router, {
        "technical": "technical",
        "fundamental": "fundamental",
        "macro": "macro",
        "selector": "selector",
    })

    def _after_technical(state: CouncilState) -> str:
        subset = state.get("analyst_subset", [])
        if "fundamental" in subset:
            return "fundamental"
        if "macro" in subset:
            return "macro"
        return "selector"

    g.add_conditional_edges("technical", _after_technical, {
        "fundamental": "fundamental",
        "macro": "macro",
        "selector": "selector",
    })

    def _after_fundamental(state: CouncilState) -> str:
        return "macro" if "macro" in state.get("analyst_subset", []) else "selector"

    g.add_conditional_edges("fundamental", _after_fundamental, {
        "macro": "macro",
        "selector": "selector",
    })

    g.add_edge("macro", "selector")

    # Selector → (drafter | END). HOLD from the Selector short-circuits the
    # graph; final_action is already set, and there is no proposal to draft.
    def _after_selector(state: CouncilState) -> str:
        return "drafter" if state.get("selected_strategy") else END

    g.add_conditional_edges("selector", _after_selector, {
        "drafter": "drafter",
        END: END,
    })

    g.add_edge("drafter", "risk_officer")
    g.add_edge("risk_officer", END)

    compiled = g.compile()

    async def _run(state: CouncilState) -> CouncilState:
        return await compiled.ainvoke(state)

    return _run


async def run_graph(
    state: CouncilState,
    *,
    llm: LLM,
    risk_caps: RiskCaps | None = None,
    force_fallback: bool = False,
    progress_cb: ProgressCallback | None = None,
    pacing_seconds: float = 0.0,
) -> CouncilState:
    """Run the council. Uses LangGraph if available + not forced off.

    When ``progress_cb`` is set (theater mode) we always take the linear
    path — it executes the exact same node coroutines in the same order
    (module docstring guarantee), and instrumenting one path beats
    surgically wrapping LangGraph internals.
    """
    caps = risk_caps or RiskCaps()
    if progress_cb is not None:
        return await _run_linear(
            state, llm, caps, progress_cb=progress_cb, pacing_seconds=pacing_seconds
        )
    if _HAS_LANGGRAPH and not force_fallback:
        runner = _build_langgraph(llm, caps)
        return await runner(state)
    return await _run_linear(state, llm, caps)
