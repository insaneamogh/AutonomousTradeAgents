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

from trading_agents.llm import LLM, Model
from trading_agents.prompts import DRAFTER
from trading_agents.state import CouncilState
from trading_agents.strategies import resolve_strategy

logger = logging.getLogger("agents.node.drafter")


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

    resp = await llm.complete(system=DRAFTER, user=user, model=Model.SONNET, max_tokens=900)
    try:
        data = LLM.parse_json(resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("drafter parse failed: %s — HOLD", exc)
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
