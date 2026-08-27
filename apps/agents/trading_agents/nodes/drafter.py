"""Drafter node — builds the proposal narrative + delegates sizing (Sonnet-tier).

The deterministic strategy-fit node already picked the strategy AND the
direction. The Drafter:
  1. Reads ``state['selected_strategy']`` / ``['selected_direction']`` +
     analyst outputs + context.
  2. Calls DRAFTER on Sonnet for verdict + bull/bear cases + risk/conviction.
  3. Hands sizing to ``engine.sizing.atr_position_size`` — the LLM's qty,
     stop, target are NEVER trusted. PLAN.md §6.3: "Never percent-of-account
     fixed; vol-target everything."

**The verdict is constrained, not trusted.** The direction came from
deterministic preconditions, so the model's only legitimate verdicts are
"the side the fit chose" or HOLD. A BUY on a short setup (or vice versa)
is not a disagreement worth honoring — it is the model contradicting the
arithmetic that selected the strategy in the first place — so it is
downgraded to HOLD in Python and logged. The model can veto; it cannot
flip.

**SELL means two different things** and conflating them is how a "close my
long" becomes an accidental short:
  - ``direction == "short"`` → SELL-TO-OPEN. Needs an inverted bracket
    (stop ABOVE entry), needs borrow, and the risk engine's short rules
    apply. Only reachable when ALLOW_SHORTS is on, because the fit node
    does not score short directions otherwise.
  - a SELL against a held long → a close. Not produced here; the position
    manager owns exits.

If Drafter says HOLD, final_action becomes HOLD and no proposal is built.
If the sizer returns qty<1, ditto — the Risk Officer never sees a proposal
it can't act on.
"""

from __future__ import annotations

import logging

from engine.sizing import SizingInputs, atr_position_size
from trading_agents.llm import LLM, Model, complete_json
from trading_agents.nodes._guards import clamp_confidence, clamp_level
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
    direction = str(state.get("selected_direction") or "long")
    required_side = "SELL" if direction == "short" else "BUY"

    ctx = state.get("context", {})
    tech = state.get("technical")
    fund = state.get("fundamental")
    macro = state.get("macro")

    parts: list[str] = [
        f"Ticker: {state['symbol']}",
        f"Chosen strategy id: {strategy_id} ({strategy_meta.display})",
        f"Strategy description: {strategy_meta.description}",
        f"DIRECTION (fixed deterministically — you may only agree or HOLD): "
        f"{direction.upper()} → the only non-HOLD verdict allowed is {required_side}",
        f"Strategy fit score: {state.get('selector_confidence', 0.0):.2f}",
        f"Fit reason: {state.get('selector_rationale', '')}",
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

    # Deterministic constraint, not a suggestion: the fit node chose the
    # side from the preconditions. A contradicting verdict is downgraded.
    if verdict != "HOLD" and verdict != required_side:
        logger.warning(
            "drafter proposed %s on %s but the deterministic fit selected a "
            "%s setup (%s) — downgrading to HOLD rather than flipping either one",
            verdict, state["symbol"], direction, strategy_id,
        )
        verdict = "HOLD"

    if verdict == "HOLD":
        # The model was asked to explain a HOLD in the bear case (see the
        # prompt), and it usually does — that explanation used to be
        # thrown away here along with the rest of ``data``, because
        # everything downstream only ever read it off ``state["proposal"]``,
        # which a HOLD never builds. Carrying it as its own state key is
        # what let a HOLD in the audit row and the theater UI stay a bare
        # "No proposal — HOLD." forever, even when the model had just
        # written three sentences about why.
        return {
            **state,
            "proposal": None,
            "final_action": "HOLD",
            "drafter_rationale": str(data.get("rationale", "")).strip(),
            "bull_case": str(data.get("bull_case", "")).strip(),
            "bear_case": str(data.get("bear_case", "")).strip(),
        }

    last_price = float(ctx.get("last_price", 100.0) or 100.0)
    equity = float(ctx.get("portfolio_equity", 100_000.0) or 100_000.0)
    atr_14 = ctx.get("technicals", {}).get("atr_14")
    # An unparseable confidence collapses to 0.0, which sizes down to
    # qty<1 and converts the pass to HOLD below — the safe direction.
    confidence = clamp_confidence(data.get("confidence", 0.5), field="drafter.confidence")

    sizing = atr_position_size(
        SizingInputs(
            symbol=str(state["symbol"]),
            last_price=last_price,
            atr_14=float(atr_14) if atr_14 is not None else None,
            account_equity=equity,
            confidence=confidence,
            # Drives the bracket geometry. On a short this puts the stop
            # ABOVE entry and the target below; the alternative fills the
            # stop the moment the order is live.
            side="SELL" if direction == "short" else "BUY",
        )
    )

    if sizing.qty < 1:
        logger.info(
            "sizer returned qty=0 for %s — converting to HOLD (%s)",
            state["symbol"], sizing.notes,
        )
        # The model actually said BUY/SELL here — it's the deterministic
        # sizer, not the drafter, that zeroed it out. That distinction
        # matters to the reader, so the sizer's OWN reason rides alongside
        # the model's bull case rather than replacing it.
        return {
            **state,
            "proposal": None,
            "final_action": "HOLD",
            "drafter_rationale": (
                f"{str(data.get('rationale', '')).strip()} | "
                f"Sizer returned 0 shares: {sizing.notes}"
            ).strip(" |"),
            "bull_case": str(data.get("bull_case", "")).strip(),
            "bear_case": str(data.get("bear_case", "")).strip(),
        }

    rationale = str(data.get("rationale", "")).strip()
    sizer_note = f"Sizing ({sizing.method}): {sizing.notes}"
    combined_rationale = f"{rationale} | {sizer_note}" if rationale else sizer_note

    # Exit plan — deterministic, disclosed at approval time. Time-stop
    # mirrors the ghost evaluator's horizon mapping so executed and
    # non-executed picks are graded over the same window.
    time_stop_days = _TIME_STOP_BY_HORIZON.get(str(state.get("horizon", "short")), 5)
    # R is a RATIO of distances, so it must be computed from absolute
    # distances — signing it off (entry - stop) yields a negative R on a
    # short, where the stop is legitimately above the entry.
    r_multiple: float | None = None
    if sizing.stop_price is not None and sizing.target_price is not None:
        risk_per_share = abs(last_price - sizing.stop_price)
        if risk_per_share > 0:
            r_multiple = round(abs(sizing.target_price - last_price) / risk_per_share, 2)

    return {
        **state,
        "proposal": {
            "strategy": strategy_id,
            "side": verdict,
            "direction": direction,
            "opens_short": direction == "short",
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
            "risk_level": clamp_level(data.get("risk_level", 3), field="risk_level"),
            "conviction_level": clamp_level(
                data.get("conviction_level", 3), field="conviction_level"
            ),
            "confidence": confidence,
            "sizing_method": sizing.method,
            # The sizing math, kept whole so the thesis view can show WHY
            # this qty / this stop / this target rather than re-deriving it.
            "sizing": {
                "method": sizing.method,
                "side": sizing.side,
                "entry_price": last_price,
                "atr_14": float(atr_14) if atr_14 is not None else None,
                "account_equity": equity,
                "confidence": confidence,
                "qty": sizing.qty,
                "stop_price": sizing.stop_price,
                "target_price": sizing.target_price,
                "risk_per_share": round(abs(last_price - sizing.stop_price), 4),
                "risk_dollars": round(
                    abs(last_price - sizing.stop_price) * sizing.qty, 2
                ),
                "notional": sizing.target_notional,
                "pct_of_equity": round(
                    (sizing.target_notional / equity) * 100.0, 3
                ) if equity > 0 else None,
                "r_multiple": r_multiple,
                "notes": sizing.notes,
            },
        },
        "final_action": verdict,
    }
