"""LangGraph wiring + a plain-async fallback.

LangGraph is the right substrate for the council when graphs branch
(parallel analyst fan-out, conditional re-runs, reflection loops back to
the Strategy Selector). For the Phase 0 linear path we run plain asyncio
when ``langgraph`` isn't installed — same node functions, same state
shape, no behavior difference.

Both code paths invoke the exact same node coroutines. Adding a new node
means: write the node in ``trading_agents.nodes``, then add it in BOTH
branches below.

**Node order, and why the first one is not an LLM.** The graph now opens
with ``strategy_fit_node`` — deterministic, no LLM, no I/O. It scores every
strategy's preconditions against the feature dict and either names a
winner or HOLDs. A HOLD there short-circuits the entire graph before the
Router has been asked anything, which means a symbol that is not a setup
costs **zero** LLM calls rather than five. On a watchlist sweep that is
most symbols on most days, so it is the dominant term in the bill.

The old Haiku Selector node is gone; ``trading_agents.nodes.strategy_fit``
explains why it was removed rather than demoted to an explainer.

**Technical/Fundamental/Macro run CONCURRENTLY, not one after another.**
They are the same shape as ``options/agents.py``'s Bull/Bear pair — three
independent reads of the SAME pre-computed feature dict, none reading
another's output, so making them wait on each other bought nothing but
wall-clock latency. ``_run_analysts_parallel`` runs whichever of the three
the Router put in ``analyst_subset`` via ``asyncio.gather``, mirroring
``run_bull_and_bear``'s pattern exactly. This is deliberately implemented
as ONE combined step (a single linear-path block / a single LangGraph
node) rather than three separate fanned-out LangGraph nodes: the three
node functions all read-modify-write the shared ``degraded_nodes`` key
(see ``nodes/_specialist.py``), and LangGraph rejects more than one write
to the same channel key per super-step unless it carries a reducer.
Merging the three results by hand in ``_merge_analyst_results`` gets the
same wall-clock win without adding a reducer to ``CouncilState`` (which
would also require touching ``router.py``/``drafter.py``, the other two
read-modify-write callers of that key, well outside this change's scope).

Live sequence:
    strategy_fit → (HOLD, 0 calls) | router → analysts (parallel) → drafter → risk
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from engine.risk import RiskCaps
from trading_agents.llm import LLM
from trading_agents.nodes import (
    drafter_node,
    fundamental_analyst_node,
    macro_analyst_node,
    risk_officer_node,
    router_node,
    strategy_fit_node,
    technical_analyst_node,
)
from trading_agents.nodes.options_council import options_agent_enabled, options_council_node
from trading_agents.progress import (
    NodeName,
    ProgressCallback,
    ProgressEvent,
    summarize_node,
)
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.graph")

try:
    from langgraph.graph import END, StateGraph  # type: ignore[import-untyped]

    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False
    logger.info("langgraph not installed — running with plain asyncio fallback")


# ─────────────────────────────────────────────────────────────────────
# Parallel analyst fan-out — shared by both code paths below.
# ─────────────────────────────────────────────────────────────────────

_AnalystFn = Callable[[CouncilState, LLM], Coroutine[Any, Any, CouncilState]]

_ANALYSTS: tuple[tuple[str, _AnalystFn], ...] = (
    ("technical", technical_analyst_node),
    ("fundamental", fundamental_analyst_node),
    ("macro", macro_analyst_node),
)


def _merge_analyst_results(
    base_state: CouncilState, names: list[str], results: list[CouncilState]
) -> CouncilState:
    """Combine N independently-run analyst results into ONE state update.

    Every analyst node's contract (``nodes/_specialist.py::run_specialist``)
    is to return ``{**state, <own_name>: {...}, "degraded_nodes": [...]}`` —
    a full copy of whatever state it was HANDED plus its own contribution.
    Because all three analysts here are handed the exact same pre-fan-out
    ``base_state`` (they run concurrently — none can see another's write),
    the only keys that can actually differ across the N results are each
    analyst's own key and ``degraded_nodes``. Reconstructing the merge by
    hand — rather than letting three LangGraph nodes write concurrently —
    is what avoids ever having two writers touch the same channel key in
    one step; see the module docstring.
    """
    merged: dict[str, Any] = dict(base_state)
    base_degraded = list(base_state.get("degraded_nodes") or [])
    degraded = list(base_degraded)
    for name, result in zip(names, results, strict=True):
        merged[name] = result.get(name)
        if name in (result.get("degraded_nodes") or []) and name not in degraded:
            degraded.append(name)
    merged["degraded_nodes"] = degraded
    return merged  # type: ignore[return-value]


async def _run_analysts_parallel(state: CouncilState, llm: LLM, subset: list[str]) -> CouncilState:
    """Run every analyst named in ``subset`` concurrently, one hop.

    Wall-clock concurrency is what matters, not just "both got awaited" —
    a sequential ``await tech(); await fund(); await macro()`` would still
    satisfy a call-count assertion while silently tripling latency for any
    pass that runs all three. ``test_analysts_run_concurrently`` asserts on
    elapsed time for exactly this reason (mirrors
    ``test_agents_run_concurrently`` in ``test_options_agents.py``).
    """
    wanted = [(name, fn) for name, fn in _ANALYSTS if name in subset]
    if not wanted:
        return state
    tasks: list[asyncio.Task[CouncilState]] = [
        asyncio.create_task(fn(state, llm)) for _, fn in wanted
    ]
    results = await asyncio.gather(*tasks)
    return _merge_analyst_results(state, [name for name, _ in wanted], list(results))


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

    # Deterministic gate FIRST. A HOLD here has cost nothing.
    await _emit("selector", "started")
    state = await strategy_fit_node(state)
    await _pace()
    await _emit("selector", "completed", with_summary=True)
    if state.get("selected_strategy") is None:
        for skipped in ("router", "technical", "fundamental", "macro", "drafter", "risk_officer"):
            await _emit(skipped, "skipped")  # type: ignore[arg-type]
        return state

    # ── Options fork ──────────────────────────────────────────────────
    # Two agents argue, then the winner calls a guarded trade tool. The
    # guard runs the FULL risk stack inside that call, so a trade made
    # here is already risk-cleared and must not be re-evaluated (or
    # re-recorded) downstream — hence risk_officer is skipped and
    # `decision_row_written` tells runtime to stand down.
    if options_agent_enabled(state):
        for skipped in ("router", "technical", "fundamental", "macro"):
            await _emit(skipped, "skipped")  # type: ignore[arg-type]
        await _emit("drafter", "started")
        state = await options_council_node(state, llm, risk_caps)
        await _pace()
        await _emit("drafter", "completed", with_summary=True)
        await _emit("risk_officer", "skipped")
        return state

    await _emit("router", "started")
    state = await router_node(state, llm)
    await _pace()
    await _emit("router", "completed", with_summary=True)

    # Concurrent, not sequential — see the module docstring. All three
    # (whichever are in ``subset``) fire "started" together, run via
    # ``asyncio.gather`` inside ``_run_analysts_parallel``, and only then
    # does anyone see "completed" — the wall-clock saving IS the point, so
    # there is no honest way to stagger these events across real time
    # without serializing the calls that produce them back out again.
    subset = state.get("analyst_subset", ["technical"])
    wanted = [name for name, _ in _ANALYSTS if name in subset]
    for analyst in wanted:
        await _emit(analyst, "started")  # type: ignore[arg-type]
    for analyst, _ in _ANALYSTS:
        if analyst not in subset:
            await _emit(analyst, "skipped")  # type: ignore[arg-type]
    if wanted:
        state = await _run_analysts_parallel(state, llm, subset)
        await _pace()
        for analyst in wanted:
            await _emit(analyst, "completed", with_summary=True)  # type: ignore[arg-type]

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

    async def _analysts(state: CouncilState) -> CouncilState:
        # ONE LangGraph node running all of technical/fundamental/macro
        # concurrently, rather than three fanned-out nodes — see the
        # module docstring for why (the shared ``degraded_nodes`` write
        # would otherwise violate LangGraph's one-writer-per-key-per-step
        # rule). A HOLD-worthy empty subset is a no-op inside the helper.
        subset = state.get("analyst_subset", ["technical"])
        return await _run_analysts_parallel(state, llm, subset)

    async def _strategy_fit(state: CouncilState) -> CouncilState:
        return await strategy_fit_node(state)

    async def _drafter(state: CouncilState) -> CouncilState:
        return await drafter_node(state, llm)

    async def _options_council(state: CouncilState) -> CouncilState:
        return await options_council_node(state, llm, risk_caps)

    async def _risk(state: CouncilState) -> CouncilState:
        state = await risk_officer_node(state, risk_caps)
        if state.get("risk_approved"):
            state["final_action"] = state.get("proposal", {}).get("side", "HOLD")  # type: ignore[union-attr]
        return state

    g.add_node("strategy_fit", _strategy_fit)
    g.add_node("router", _router)
    g.add_node("analysts", _analysts)
    g.add_node("drafter", _drafter)
    g.add_node("risk_officer", _risk)
    g.add_node("options_council", _options_council)

    g.set_entry_point("strategy_fit")

    # Deterministic HOLD short-circuit — nothing downstream has run yet, so
    # this is the branch that makes a non-setup symbol free.
    def _after_strategy_fit(state: CouncilState) -> str:
        if not state.get("selected_strategy"):
            return END
        # Options fork — see _run_linear's twin branch and
        # nodes/options_council.py for why risk_officer is NOT downstream
        # of this one (the guard already ran the full stack inline).
        if options_agent_enabled(state):
            return "options_council"
        return "router"

    g.add_conditional_edges("strategy_fit", _after_strategy_fit, {
        "router": "router",
        "options_council": "options_council",
        END: END,
    })
    g.add_edge("options_council", END)

    # Single hop for all three analysts — ``_analysts`` runs whichever of
    # technical/fundamental/macro the Router put in ``analyst_subset``
    # concurrently (see the module docstring), so there is nothing left to
    # branch on here: every non-options pass goes router -> analysts ->
    # drafter, and an empty subset just makes ``_analysts`` a no-op.
    g.add_edge("router", "analysts")
    g.add_edge("analysts", "drafter")

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
    caps = risk_caps or RiskCaps.from_env()
    if progress_cb is not None:
        return await _run_linear(
            state, llm, caps, progress_cb=progress_cb, pacing_seconds=pacing_seconds
        )
    if _HAS_LANGGRAPH and not force_fallback:
        runner = _build_langgraph(llm, caps)
        return await runner(state)
    return await _run_linear(state, llm, caps)
