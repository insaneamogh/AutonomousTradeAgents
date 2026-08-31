"""Bull/Bear options agents — docs/IMPL_OPTIONS_AGENTS.md §3.

Sequencing (this is the part that is easy to get backwards — read
``docs/PLAN_OPTIONS_AGENTS.md`` §0's diagram before changing it):

  1. Bull and Bear each read the SAME deterministic pre-pass and form an
     independent view (direction/strategy/conviction/thesis) — ONE hop,
     run in PARALLEL via ``asyncio.gather``, no tool calls at all. Neither
     sees the other's output; anchoring would make the second opinion
     worthless (``run_bull_and_bear``).
  2. ``resolution.resolve()`` combines the two views. This is the only
     place they combine, and it is plain Python — no LLM involved.
  3. ONLY when ``resolution.proceed``, a SECOND hop lets the Bull agent
     (never Bear) actually call ``open_option_trade``, via
     ``llm_loop.run_tool_loop`` + ``tools.guard.dispatch_tool_call``, now
     carrying the RESOLVED direction/conviction in the ``GuardContext`` —
     not Bull's own pre-resolution numbers (``run_options_agents``).

Neither agent ever supplies a strike, expiry, OCC symbol or quantity —
even in the tool-calling hop, ``tools/guard.py`` derives those.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from engine.risk import RiskCaps
from trading_agents.llm import LLM, LLMResponse, Model, ToolCall, complete_json
from trading_agents.llm_loop import run_tool_loop
from trading_agents.nodes._guards import clamp_confidence
from trading_agents.options.prompts import OPTIONS_BEAR, OPTIONS_BULL
from trading_agents.options.resolution import AgentView, Resolution, resolve
from trading_agents.options.tools.guard import GuardContext, ToolGuard, dispatch_tool_call
from trading_agents.options.tools.registry import REGISTRY
from trading_agents.options.tools.schemas import OPEN_OPTION_TRADE, READ_ONLY_TOOLS
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.options.agents")

__all__ = [
    "OptionsAgentsResult",
    "run_bear",
    "run_bull",
    "run_bull_and_bear",
    "run_options_agents",
]

#: docs/IMPL_OPTIONS_AGENTS.md §6 OPTIONS_AGENT_MAX_ROUNDS default — the
#: tool loop's own round budget (propose -> denial feedback -> retry once
#: -> a final text turn), independent of anything in this file.
DEFAULT_MAX_ROUNDS = 3

_VIEW_MAX_TOKENS = 500
_TRADE_MAX_TOKENS = 1024

_VALID_DIRECTIONS = frozenset({"long", "short"})

# Both agents get every read-only tool in the trade hop EXCEPT the trade
# tool itself is bull-only; read-only tools carry no risk (tools/guard.py's
# read-only branch: `GuardVerdict(True, None, {})` unconditionally) so
# there is nothing unsafe about Bull optionally checking one before it
# commits to a tool call.
_BULL_TRADE_TOOLS: tuple[dict[str, Any], ...] = (OPEN_OPTION_TRADE, *READ_ONLY_TOOLS)


def _parse_direction(value: Any) -> str | None:
    """Anything other than exactly "long"/"short" (case/whitespace
    insensitive) reads as "no view" rather than raising — a model that
    answers "bullish" instead of "long" should stand down, not crash the
    pass."""
    if isinstance(value, str) and value.strip().lower() in _VALID_DIRECTIONS:
        return value.strip().lower()
    return None


def _parse_strategy(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _render_pre_pass(state: CouncilState) -> str:
    """The evidence block both agents read — IDENTICAL for both, so
    neither sees anything the other doesn't (docs/IMPL_OPTIONS_AGENTS.md
    §3.2: "a COMPLETE deterministic pre-pass"). Every field is read
    defensively (``n/a`` on absence) — this pre-pass is assembled upstream
    of this module's scope, and a missing block must degrade to "less
    evidence", never to a crashed pass.
    """
    context: dict[str, Any] = state.get("context", {}) or {}
    lines = [
        f"Ticker: {state.get('symbol', '?')}",
        f"Horizon: {state.get('horizon', 'short')}",
        "",
    ]

    if state.get("strategy_fit"):
        lines.append("Strategy fit (deterministic pre-pass, not binding on you):")
        lines.append(f"  selected_strategy: {state.get('selected_strategy', 'n/a')}")
        lines.append(f"  selector_confidence: {state.get('selector_confidence', 'n/a')}")
        lines.append(f"  selector_rationale: {state.get('selector_rationale', 'n/a')}")
        lines.append("")

    patterns = context.get("patterns")
    if patterns:
        lines.append("Candlestick patterns (already ATR-normalised, trend-context-gated):")
        for key in (
            "top_pattern", "top_pattern_score", "reversal_bull", "reversal_bear",
            "continuation_bull", "continuation_bear", "indecision", "compression", "expansion",
        ):
            lines.append(f"  {key}: {patterns.get(key, 'n/a')}")
        lines.append("")

    quant = context.get("quant") or {}
    lines.append("Volatility:")
    lines.append(f"  realized_vol_pct: {quant.get('realized_vol_pct', 'n/a')}")
    # iv_rank is not yet a pre-pass FEATURE anywhere in this codebase as of
    # this writing — only a live get_iv_rank TOOL exists (tools/readonly.py),
    # and only the Bull agent's second (tool-calling) hop can reach it.
    # docs/PLAN_OPTIONS_AGENTS.md §7/§9 lists building a real pre-pass
    # iv_rank feature as a separate, not-yet-done ~1h item. Read
    # defensively so this renders "n/a" today and picks the real feature up
    # automatically the moment a future pass starts populating it, with no
    # change needed here.
    lines.append(f"  iv_rank: {context.get('iv_rank', 'n/a')}")
    lines.append("")

    funnel = state.get("contract_funnel")
    if funnel:
        lines.append("Option-chain funnel (most recent, this pass):")
        lines.append(f"  {funnel}")
        lines.append("")

    return "\n".join(lines)


async def _run_view(*, role: str, system: str, state: CouncilState, llm: LLM) -> AgentView:
    """One agent's independent, tool-free view. Shared body for Bull/Bear —
    the only difference between them is which system prompt is passed in,
    matching ``nodes/_specialist.py``'s "same body, different prompt"
    convention for the equity analysts.
    """
    user = _render_pre_pass(state)
    data, degraded = await complete_json(
        llm,
        system=system,
        user=user,
        model=Model.SONNET,
        max_tokens=_VIEW_MAX_TOKENS,
        council_run_id=state.get("council_run_id"),
        user_id=state.get("user_id"),
    )
    if data is None:
        logger.warning("options.%s: degraded — standing down for this pass", role)
        return AgentView(role=role, direction=None, conviction=0.0, thesis="", degraded=True)

    return AgentView(
        role=role,
        direction=_parse_direction(data.get("direction")),
        conviction=clamp_confidence(data.get("conviction", 0.0), field=f"options_{role}.conviction"),
        thesis=str(data.get("thesis") or ""),
        strategy=_parse_strategy(data.get("strategy")),
        degraded=degraded,
    )


async def run_bull(state: CouncilState, llm: LLM) -> AgentView:
    """Bull's independent view. No tool calls — see module docstring."""
    return await _run_view(role="bull", system=OPTIONS_BULL, state=state, llm=llm)


async def run_bear(state: CouncilState, llm: LLM) -> AgentView:
    """Bear's independent view. No tool calls — see module docstring."""
    return await _run_view(role="bear", system=OPTIONS_BEAR, state=state, llm=llm)


async def run_bull_and_bear(state: CouncilState, llm: LLM) -> tuple[AgentView, AgentView]:
    """Both views, in PARALLEL, one hop.

    Wall-clock concurrency is what matters here, not just "both got
    awaited" — a sequential ``await run_bull(); await run_bear()`` would
    still satisfy a call-count assertion while silently doubling latency.
    ``test_agents_run_concurrently`` in ``test_options_agents.py`` asserts
    on elapsed time for exactly this reason (docs/IMPL_OPTIONS_AGENTS.md
    §3.3, PLAN doc §11.6).
    """
    bull_task = asyncio.create_task(run_bull(state, llm))
    bear_task = asyncio.create_task(run_bear(state, llm))
    return await asyncio.gather(bull_task, bear_task)


def _trade_hop_user_message(state: CouncilState, *, bull: AgentView, resolution: Resolution) -> str:
    """Bull's second-hop prompt: the same pre-pass evidence plus its own
    prior (hop-1) view and the deterministic resolution — so the model is
    re-affirming a view it already committed to, not starting cold, and
    is explicitly told the RESOLVED conviction it is bound by (the guard
    enforces this independently at the tool boundary; telling the model
    is what keeps a plain retry from wasting a round arguing for a number
    that will just be clamped back down anyway).
    """
    return (
        _render_pre_pass(state)
        + "\n\nYour own view from the argument phase: direction="
        + f"{bull.direction!r} strategy={bull.strategy!r} conviction={bull.conviction:.2f} "
        + f"thesis={bull.thesis!r}\n"
        + "The Bear Agent's independent view agreed with you on direction. "
        + "The resolved conviction for this trade is the LOWER of your two "
        + f"numbers: {resolution.conviction:.2f}.\n\n"
        + "Call open_option_trade now if you still want to act on this — "
        + "restate direction, strategy, conviction and thesis (conviction "
        + "above this pass's resolved value will be capped, not rejected). "
        + "Or explain in text why you are standing down instead."
    )


@dataclass(frozen=True)
class OptionsAgentsResult:
    bull: AgentView
    bear: AgentView
    resolution: Resolution
    trade_response: LLMResponse | None
    """The tool-calling hop's final LLM turn, or ``None`` when resolution
    never proceeded (no second hop happens at all — see module docstring
    point 3)."""
    tool_transcript: tuple[dict[str, Any], ...]
    """Every tool call the trade hop made, in order — each entry shaped
    ``{"tool": str, "input": dict, "output": dict}`` (``llm_loop.
    run_tool_loop``'s own transcript shape). Empty when there was no
    second hop."""


async def run_options_agents(
    state: CouncilState,
    llm: LLM,
    *,
    guard: ToolGuard,
    caps: RiskCaps,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> OptionsAgentsResult:
    """The whole two-hop pass: parallel argument, deterministic resolution,
    then — only on ``resolve().proceed`` — the Bull agent's guarded
    tool-calling hop. A HOLD (disagreement/abstain/divergence) never
    reaches a second hop at all: zero extra LLM calls for a pass that was
    never going to trade (docs/PLAN_OPTIONS_AGENTS.md §8's latency table).
    """
    bull, bear = await run_bull_and_bear(state, llm)
    resolution = resolve(bull, bear)

    if not resolution.proceed:
        return OptionsAgentsResult(bull, bear, resolution, None, ())

    user_id = str(state.get("user_id") or "")
    council_run_id = str(state.get("council_run_id") or "")

    # Mutable-by-closure call counter — GuardContext itself is frozen
    # (tools/guard.py), and a NEW one is built per attempt so
    # `calls_this_pass` reflects attempts so far in THIS pass, not a
    # snapshot taken once before the loop started. Only a SUCCESSFUL open
    # increments it: the guard's schema explicitly allows the model to
    # "adjust once" after a denial (docs/IMPL_OPTIONS_AGENTS.md §1's
    # OPEN_OPTION_TRADE description), so a denied first attempt must not
    # burn the pass's one-open budget — only an actual fill should.
    calls_this_pass = 0

    async def _dispatch(call: ToolCall) -> dict[str, Any]:
        nonlocal calls_this_pass
        ctx = GuardContext(
            user_id=user_id,
            council_run_id=council_run_id,
            resolved_direction=resolution.direction,
            resolved_conviction=resolution.conviction,
            calls_this_pass=calls_this_pass,
            caps=caps,
        )
        result = await dispatch_tool_call(call, ctx, guard=guard, registry=REGISTRY)
        if call.name == "open_option_trade" and not result.get("is_error"):
            calls_this_pass += 1
        return result

    trade_response, transcript = await run_tool_loop(
        llm,
        system=OPTIONS_BULL,
        user=_trade_hop_user_message(state, bull=bull, resolution=resolution),
        tools=list(_BULL_TRADE_TOOLS),
        dispatch=_dispatch,
        model=Model.SONNET,
        max_rounds=max_rounds,
        max_tokens=_TRADE_MAX_TOKENS,
        council_run_id=state.get("council_run_id"),
        user_id=state.get("user_id"),
    )
    return OptionsAgentsResult(bull, bear, resolution, trade_response, tuple(transcript))
