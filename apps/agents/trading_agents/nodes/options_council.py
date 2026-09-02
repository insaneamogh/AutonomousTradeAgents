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


def _attempted_trade(transcript: tuple[dict[str, Any], ...]) -> bool:
    """Did the model emit an ``open_option_trade`` call AT ALL this pass —
    successful, denied, or malformed?

    This is the question that separates a DECISION from a FAILURE. Both
    end the pass with no position, and until this existed both rendered
    as the same "agents agreed but chose not to open" line, so a model
    that could not drive the tool loop was indistinguishable from a market
    with nothing worth trading. On a book capped at five concurrent
    positions, that ambiguity can hide a dead desk for a whole session.

    Deliberately counts DENIED attempts as attempts: a denial means the
    model formed a well-shaped call and the deterministic guard refused
    it, which is the system working. Only the complete ABSENCE of a call,
    on a pass the resolver said should proceed, indicts the model.
    """
    return any(e.get("tool") == "open_option_trade" for e in transcript)


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
    # ToolGuard() with no arguments resolves its own production
    # dependencies (Postgres risk context, the real decision log, a
    # broker factory) — see its __init__. Tests inject fakes instead.
    guard = ToolGuard()

    # DETERMINISTIC PRE-FLIGHT, BEFORE ANY PAID CALL.
    #
    # Every account-level options gate used to run only inside
    # `guard.before()`, which the tool loop reaches AFTER
    # `run_bull_and_bear`'s two Sonnet calls and the trade hop's third. So
    # a symbol that could not possibly trade still cost a full paid debate
    # to find that out. Measured 2026-09-01: 293 options council runs, 7
    # traded, 48 refused `max_total_premium_pct` — a portfolio-level fact
    # that has nothing to do with the symbol or with anything either agent
    # said. The book hit its cap at 15:00 UTC and stayed there; every
    # options pass for the next three hours paid ~$0.025 to be told so.
    #
    # This asks the symbol-INDEPENDENT half of that question first, for
    # free, and HOLDs with the same named rule the guard itself would have
    # returned — so the audit row and the Refusal Ledger read identically
    # whether the refusal came from here or from the real check.
    # `preflight_can_open` fails OPEN on infrastructure trouble, so a
    # broken pre-flight degrades to the normal paid path, never to a
    # silent HOLD.
    try:
        preflight = await guard.preflight_can_open(
            user_id=str(state.get("user_id") or ""), caps=caps
        )
    except Exception:
        logger.exception(
            "options preflight raised for %s — continuing to the normal path",
            state.get("symbol"),
        )
        preflight = None

    # Second pre-flight: the CHAIN itself, still before any paid call.
    # `select_contract` normally runs inside the tool guard — i.e. after
    # both debate calls and the trade hop — so an untradeable chain cost
    # ~3 model calls to discover. Conviction picks between exactly two
    # delta bands, so testing both covers every value the agents could
    # reach; see `preflight_chain_is_tradeable`. Only runs when the
    # account-level pre-flight already passed, so a halted/closed/capped
    # account never pays for a chain fetch either.
    if preflight is not None and preflight.allow:
        direction = str(state.get("selected_direction") or "")
        underlying = str(state.get("symbol") or "")
        if direction in ("long", "short") and underlying:
            try:
                preflight = await guard.preflight_chain_is_tradeable(
                    underlying=underlying, direction=direction, caps=caps
                )
            except Exception:
                logger.exception(
                    "options chain preflight raised for %s — continuing to the "
                    "normal path", underlying,
                )

    if preflight is not None and not preflight.allow:
        reason = preflight.reason or "preflight_refused"
        logger.info(
            "options council SKIPPED for %s — %s (deterministic pre-flight, "
            "0 LLM calls)", state.get("symbol"), reason,
        )
        # `preflight.payload` carries whichever of risk_veto_rule /
        # contract_funnel this specific reason actually earned — see
        # guard.py's own docstrings on preflight_can_open /
        # preflight_chain_is_tradeable for exactly which reasons carry
        # which key. Read generically rather than re-deriving the
        # classification here: until 2026-09-02 this branch set neither
        # field, so a preflight-skipped symbol was invisible to both the
        # veto ledger (`risk_veto_rule IS NOT NULL`) and the funnel report
        # (skips rows with no `contract_funnel`) even though it was a real,
        # named refusal — the more this optimisation saved, the blinder
        # the Refusal Ledger got to options. risk_checks_passed is
        # deliberately NEVER set here: neither preflight runs evaluate(),
        # so nothing here has "passed" a risk check in that field's sense.
        payload = preflight.payload or {}
        return {
            **state,
            "final_action": "HOLD",
            "proposal": None,
            "risk_approved": False,
            "risk_veto_rule": payload.get("risk_veto_rule"),
            "contract_funnel": payload.get("contract_funnel"),
            "tool_denials": [f"preflight:{reason}"],
            "drafter_rationale": (
                f"Refused before any model call by the deterministic "
                f"pre-flight: {reason}."
            ),
        }

    try:
        result = await run_options_agents(state, llm, guard=guard, caps=caps)
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
    elif not _attempted_trade(result.tool_transcript):
        # The resolver said proceed, the trade hop ran, and no
        # open_option_trade call ever came out of it. That is a
        # TOOL-CALLING failure, not a judgement, and it must never again
        # be reported in the same words as a deliberate stand-down:
        # the two have opposite remedies (change the model / prompt vs.
        # trust the desk) and identical symptoms.
        why = (
            "Trade hop produced no open_option_trade call — tool-calling "
            "failure, not a decision. The agents agreed to trade "
            f"{resolution.direction} at conviction "
            f"{resolution.conviction:.2f} and the hop ended without asking."
        )
        out["tool_denials"] = [*denials, "open_option_trade:no_call_emitted"]
        logger.warning(
            "options council: %s agreed (%s @ %.2f) but the trade hop emitted "
            "NO open_option_trade call — check OPTIONS_AGENT_MODEL tool-calling",
            state.get("symbol"), resolution.direction, resolution.conviction,
        )
    else:
        why = "Agents agreed but chose not to open a position."

    out["final_action"] = "HOLD"
    out["proposal"] = None
    out["drafter_rationale"] = why
    logger.info("options council HOLD for %s — %s", state.get("symbol"), why)
    return out
