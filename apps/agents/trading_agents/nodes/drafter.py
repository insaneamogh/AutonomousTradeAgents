"""Drafter node — builds the proposal narrative + delegates sizing (Sonnet-tier).

PLAN.md §5.1 splits Strategy Selector from Proposal Drafter. The Selector
already picked an id; the Drafter:
  1. Reads ``state['selected_strategy']`` + analyst outputs + context.
  2. Calls DRAFTER on Sonnet for verdict + bull/bear cases + risk/conviction.
  3. Hands sizing to ``engine.sizing.atr_position_size`` — the LLM's qty,
     stop, target are NEVER trusted. PLAN.md §6.3: "Never percent-of-account
     fixed; vol-target everything."

If Drafter says HOLD (post-Selector), final_action becomes HOLD and no
proposal is built. If the sizer returns qty<1, ditto — the Risk Officer
never sees a proposal it can't act on.
"""

from __future__ import annotations

import logging

from engine.sizing import SizingInputs, atr_position_size

from trading_agents.llm import LLM, Model, complete_json
from trading_agents.prompts import DRAFTER
from trading_agents.state import CouncilState
from trading_agents.strategies import resolve_strategy

logger = logging.getLogger("agents.node.drafter")

# Time-stop per horizon — IDENTICAL to the ghost evaluator's horizon map so
# executed and non-executed picks are graded over the same window.
_TIME_STOP_BY_HORIZON: dict[str, int] = {
    "intraday": 1,
    "short": 5,
    "mid": 10,
    "long": 20,
}


async def drafter_node(state: CouncilState, llm: LLM) -> CouncilState:
    strategy_id = state.get("selected_strategy")
    if not strategy_id:
        # Defensive guard. Graph wiring should skip Drafter when Selector
        # held; if we got here anyway, mirror the Selector's HOLD.
        logger.info("drafter invoked without selected_strategy — HOLD")
        return {**state, "proposal": None, "final_action": "HOLD"}

    strategy_meta = resolve_strategy(strategy_id)

    ctx = state.get("context", {})
    tech = state.get("technical")
    fund = state.get("fundamental")
    macro = state.get("macro")

    parts: list[str] = [
        f"Ticker: {state['symbol']}",
        f"Chosen strategy id: {strategy_id} ({strategy_meta.display})",
        f"Strategy description: {strategy_meta.description}",
        f"Selector confidence: {state.get('selector_confidence', 0.0):.2f}",
        f"Selector rationale: {state.get('selector_rationale', '')}",
        f"Horizon: {state.get('horizon', 'short')}",
        f"Regime: {state.get('regime', 'unknown')}",
        f"Last price: {ctx.get('last_price', 'n/a')}",
        f"Portfolio equity: {ctx.get('portfolio_equity', 'n/a')}",
        "",
    ]
    if tech:
        parts.append(
            f"Technical analyst: score={tech.get('score', 'n/a')} "
            f"conf={tech.get('confidence', 'n/a')} thesis=\"{tech.get('thesis', '')}\""
        )
    if fund:
        parts.append(
            f"Fundamental analyst: score={fund.get('score', 'n/a')} "
            f"conf={fund.get('confidence', 'n/a')} thesis=\"{fund.get('thesis', '')}\""
        )
    if macro:
        parts.append(
            f"Macro analyst: score={macro.get('score', 'n/a')} "
            f"conf={macro.get('confidence', 'n/a')} thesis=\"{macro.get('thesis', '')}\""
        )
    user = "\n".join(parts) + "\n"

    data, degraded = await complete_json(
        llm,
        system=DRAFTER, user=user, model=Model.SONNET, max_tokens=900
    )
    if degraded:
        state = {**state, "degraded_nodes": [*(state.get("degraded_nodes") or []), "drafter"]}
    if data is None:
        logger.warning("drafter degraded — HOLD")
        return {**state, "proposal": None, "final_action": "HOLD"}

    verdict = str(data.get("verdict", "HOLD")).upper()
    if verdict not in ("BUY", "SELL", "HOLD"):
        verdict = "HOLD"

    if verdict == "HOLD":
        return {**state, "proposal": None, "final_action": "HOLD"}

    last_price = float(ctx.get("last_price", 100.0) or 100.0)
    equity = float(ctx.get("portfolio_equity", 100_000.0) or 100_000.0)
    atr_14 = ctx.get("technicals", {}).get("atr_14")
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))

    sizing = atr_position_size(
        SizingInputs(
            symbol=str(state["symbol"]),
            last_price=last_price,
            atr_14=float(atr_14) if atr_14 is not None else None,
            account_equity=equity,
            confidence=confidence,
        )
    )

    if sizing.qty < 1:
        logger.info(
            "sizer returned qty=0 for %s — converting to HOLD (%s)",
            state["symbol"], sizing.notes,
        )
        return {**state, "proposal": None, "final_action": "HOLD"}

    rationale = str(data.get("rationale", "")).strip()
    sizer_note = f"Sizing ({sizing.method}): {sizing.notes}"
    combined_rationale = f"{rationale} | {sizer_note}" if rationale else sizer_note

    # Exit plan — deterministic, disclosed at approval time. Time-stop
    # mirrors the ghost evaluator's horizon mapping so executed and
    # non-executed picks are graded over the same window.
    time_stop_days = _TIME_STOP_BY_HORIZON.get(str(state.get("horizon", "short")), 5)
    r_multiple: float | None = None
    if sizing.stop_price is not None and sizing.target_price is not None:
        risk_per_share = last_price - sizing.stop_price
        if risk_per_share > 0:
            r_multiple = round((sizing.target_price - last_price) / risk_per_share, 2)

    return {
        **state,
        "proposal": {
            "strategy": strategy_id,
            "side": verdict,
            "qty": sizing.qty,
            "order_type": "MARKET",
            "estimated_notional": sizing.target_notional,
            "stop_loss": sizing.stop_price,
            "target_price": sizing.target_price,
            "time_stop_days": time_stop_days,
            "r_multiple": r_multiple,
            "rationale": combined_rationale,
            "bull_case": str(data.get("bull_case", "")),
            "bear_case": str(data.get("bear_case", "")),
            "risk_level": int(data.get("risk_level", 3)),
            "conviction_level": int(data.get("conviction_level", 3)),
            "confidence": confidence,
            "sizing_method": sizing.method,
        },
        "final_action": verdict,
    }
