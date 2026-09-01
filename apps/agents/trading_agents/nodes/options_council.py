"""The options council node — Bull/Bear argument, then a guarded trade.

This is the seam that makes ``options.agents.run_options_agents`` reachable
from a real pass. Before this module it was fully built, fully tested, and
called from **nowhere but its own tests**: ``USE_OPTIONS_AGENT`` was read by
no production code, so every options pass went through the shared
equity council (strategy_fit -> router -> analysts -> drafter) regardless.

Where it sits in the graph
--------------------------
``strategy_fit`` runs first and is deterministic — a symbol with no setup
HOLDs here having spent zero tokens, exactly as before. Only when that gate
passes AND the pass is options-flavoured AND the flag is on does this node
replace the router/analysts/drafter leg. Everything downstream
(``risk_officer``) is unchanged, because by the time this node returns, any
trade it made has *already* been through the full risk stack inside
``ToolGuard.before`` — see ``options/tools/guard.py``.

The double-write hazard, and why ``decision_row_written`` exists
----------------------------------------------------------------
``options.tools.trade.open_option_trade`` persists its OWN
``agent_decisions`` row, keyed on ``ctx.council_run_id`` — the same id
``runtime.run_council`` uses for the row IT writes at the end of a pass.
Left alone, the council's row would land on top of the trade's row and
replace a real executed options trade with a summary that knows nothing
about the fill. So this node sets ``decision_row_written`` on the state and
``runtime`` skips its own record when it sees it. That flag is load-bearing;
do not drop it because it looks like bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.env import env_flag
from engine.risk import RiskCaps
from trading_agents.llm import LLM
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.node.options_council")


def options_agent_enabled(state: CouncilState) -> bool:
    """Whether THIS pass should use the two-agent options council.

    Three conditions, all required:

    1. ``USE_OPTIONS_AGENT`` — the operator's switch. Off ⇒ the shared
       equity council keeps handling options exactly as it does today, so
       flipping this off is a complete, instant rollback.
    2. ``state["instrument"] == "option"`` — set by ``strategy_fit_node``
       only when ``ALLOW_OPTIONS`` **and** the watchlist row's
       ``asset_class`` say so. An equity pass must never be routed here.
    3. A strategy actually fit. Guaranteed by the caller, asserted here so
       this function is safe to call from either graph branch.
    """
    if not env_flag("USE_OPTIONS_AGENT"):
        return False
    if state.get("instrument") != "option":
        return False
    return state.get("selected_strategy") is not None


def _traded(transcript: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    """The successful ``open_option_trade`` result, if there was one.

    A denial comes back through the same transcript with ``is_error`` set
    (the guard NEVER raises — see ``dispatch_tool_call``), so "the model
    called the tool" and "a trade happened" are different questions and
    this asks the second one.
    """
    for entry in transcript:
        if entry.get("tool") != "open_option_trade":
            continue
        out = entry.get("output") or {}
        if out.get("is_error"):
            continue
        content = out.get("content") or {}
        if isinstance(content, dict) and content.get("decision_id"):
            return content
    return None


def _denials(transcript: tuple[dict[str, Any], ...]) -> list[str]:
    """Every named refusal the guard returned this pass.

    Surfaced onto the state because "the agent asked to open NVDA calls and
    the risk engine said `illiquid_contract`" is the propose/dispose story
    in one line — and it is otherwise invisible outside the log.
    """
    out: list[str] = []
    for entry in transcript:
        result = entry.get("output") or {}
        if not result.get("is_error"):
            continue
        content = result.get("content") or {}
        denied = content.get("denied") if isinstance(content, dict) else None
        if denied:
            out.append(f"{entry.get('tool')}:{denied}")
    return out


def _contract_funnel(transcript: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    """The ``contract_funnel`` block off the last ``open_option_trade`` call
    this pass, whether it was denied or succeeded — or ``None`` when the
    model never called the tool at all (no direction resolution, agents
    disagreed, etc.), matching ``nodes/drafter.py``'s own "claiming a
    funnel would be fabricating a stage that never ran" rule.

    Fixes the gap this whole module used to have: ``ToolGuard.
    _before_open_option_trade`` runs ``select_contract`` on EVERY attempted
    open (guard.py), but until 2026-09-01 nothing carried its funnel_counts
    past the tool call — only the bare rejection-reason string survived,
    inside ``drafter_rationale``'s free text. ``dispatch_tool_call`` now
    folds ``verdict.payload`` into a denial's ``content`` (see guard.py),
    and ``trade.py``'s successful-open row already carries its own copy —
    this just lifts either one back onto state so `runtime`'s normal HOLD
    write persists it via `reasoning.contract_funnel`, exactly like the
    legacy drafter.py options path already does. A successful trade's copy
    here is redundant with `trade.py`'s own row (that row is what actually
    gets persisted, per `decision_row_written`) but costs nothing to set.
    """
    for entry in transcript:
        if entry.get("tool") != "open_option_trade":
            continue
        content = (entry.get("output") or {}).get("content")
        if isinstance(content, dict) and isinstance(content.get("contract_funnel"), dict):
            return content["contract_funnel"]
    return None


async def options_council_node(
    state: CouncilState,
    llm: LLM,
    risk_caps: RiskCaps | None = None,
) -> CouncilState:
    """Run the two-agent options council and fold the result into state.

    Never raises into the graph: any failure degrades to a HOLD with a
    named reason, exactly like every other node here. An options pass that
    blows up must not take down the scheduled sweep for every other symbol.
    """
    from trading_agents.options.agents import run_options_agents
    from trading_agents.options.tools.guard import ToolGuard

    caps = risk_caps or RiskCaps.from_env()

    try:
        # ToolGuard() with no arguments resolves its own production
        # dependencies (Postgres risk context, the real decision log, a
        # broker factory) — see its __init__. Tests inject fakes instead.
        result = await run_options_agents(state, llm, guard=ToolGuard(), caps=caps)
    except Exception:
        logger.exception("options council failed for %s — HOLDing", state.get("symbol"))
        return {
            **state,
            "final_action": "HOLD",
            "proposal": None,
            "drafter_rationale": "Options council errored; no trade.",
        }

    bull, bear, resolution = result.bull, result.bear, result.resolution
    denials = _denials(result.tool_transcript)

    out: CouncilState = {
        **state,
        "bull_case": bull.thesis or "",
        "bear_case": bear.thesis or "",
        "contract_funnel": _contract_funnel(result.tool_transcript),
        "selector_confidence": resolution.conviction,
        "options_resolution": {
            "proceed": resolution.proceed,
            "reason": resolution.reason,
            "direction": resolution.direction,
            "conviction": resolution.conviction,
            "bull": {"direction": bull.direction, "conviction": bull.conviction,
                     "degraded": bull.degraded},
            "bear": {"direction": bear.direction, "conviction": bear.conviction,
                     "degraded": bear.degraded},
        },
        "tool_denials": denials,
    }

    trade = _traded(result.tool_transcript)
    if trade is not None:
        # The trade tool already wrote the audit row AND already cleared the
        # full risk stack inside the guard. Mark both so runtime skips its
        # own write and risk_officer skips a second evaluation of a position
        # that is already open at the broker.
        out["final_action"] = "BUY"
        out["decision_row_written"] = True
        out["decision_id"] = str(trade.get("decision_id"))
        out["risk_approved"] = True
        out["risk_reason"] = "Cleared the options risk stack inside the tool guard."
        out["risk_checks_passed"] = list(trade.get("checks_passed") or [])
        out["drafter_rationale"] = (
            f"Both agents agreed {resolution.direction} at conviction "
            f"{resolution.conviction:.2f}; opened {trade.get('occ_symbol')} "
            f"x{trade.get('qty')}."
        )
        logger.info(
            "options council OPENED %s x%s for %s (order=%s)",
            trade.get("occ_symbol"), trade.get("qty"),
            state.get("symbol"), trade.get("order_id"),
        )
        return out

    # No trade. Say WHY in a form the UI can render — a bare HOLD is the
    # complaint this whole surface exists to answer.
    if denials:
        why = f"Refused by the risk guard: {', '.join(denials)}."
    elif not resolution.proceed:
        why = f"Agents did not agree ({resolution.reason})."
    else:
        why = "Agents agreed but chose not to open a position."

    out["final_action"] = "HOLD"
    out["proposal"] = None
    out["drafter_rationale"] = why
    logger.info("options council HOLD for %s — %s", state.get("symbol"), why)
    return out
