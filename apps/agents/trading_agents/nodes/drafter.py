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

**Options Phase A** (``state["instrument"] == "option"``, set by
``strategy_fit_node`` — see ``docs/OPTIONS_PLAN.md``): once the verdict is
fixed (BUY/SELL, matching the deterministic direction) and is not HOLD,
sizing is handed to ``engine.options.selection.select_contract`` +
``engine.options.sizing.options_position_size`` INSTEAD OF
``atr_position_size`` — an option's risk is the whole premium, not an ATR
stop distance. No liquid contract → a named HOLD, never a silent fallback
to the equity path. On success the built ``side`` is always "BUY": Phase A
only ever buys a call or a put to open (``OptionLegDetails.action`` is
always ``buy_to_open``), even for a bearish ("short") thesis — that thesis
buys a PUT, it does not sell anything short. This is deliberate and
load-bearing: ``engine.risk.rules._short.opens_short`` (and everything
downstream of it — ``short_requires_stop``, ``short_unbounded_loss_cap``)
only fires when ``RiskProposal.side is Side.SELL``, and an options proposal
carries ``stop_loss=None`` (Alpaca has no bracket for options), which would
otherwise look exactly like the "short with no stop" case those rules
exist to catch. ``direction`` ("long"/"short") still carries the THESIS,
separately from ``side``/``option_action`` which carry the order mechanics.

The options chain fetch (``_fetch_option_candidates`` below) delegates to
``engine.options.contracts.fetch_option_candidates`` — a lazy import
(mirroring ``engine.features.provider.AlpacaAssetInfoProvider``'s own
lazy-import convention for broker-specific calls): importing this module
never fails because of it, and a missing/failing chain fetch degrades to
zero candidates, which ``select_contract`` turns into a named
``no_candidates`` HOLD — never a crash, never a silent equity fallback.

**History, for whoever next touches this file**: the chain fetch used to
live directly here, calling ``broker.alpaca.list_option_contracts`` (the
*contract-metadata* endpoint, ``/v2/options/contracts``) with an adapter
that read ``bid``/``ask``/``delta``/``implied_volatility`` off the raw
result via ``getattr(..., None)``. Those attributes never existed on that
endpoint's real response — bid/ask/greeks/IV live on a DIFFERENT Alpaca
endpoint entirely, the chain SNAPSHOT
(``docs/OPTIONS_PLAN.md`` §0's live-verified
``/v1beta1/options/snapshots/{underlying}``) — so every real contract
silently became ``None`` and was filtered out. Options trading was
therefore completely inert against any real account: `no_candidates`
every time, regardless of signal quality, invisible across 736 passing
tests because none of them exercised this boundary with a real Alpaca
shape. Fixed by moving the fetch + field-mapping to
``engine.options.contracts.fetch_option_candidates`` (which calls the
correct endpoint via ``broker.alpaca.list_option_chain_quotes``) — see
that function's own docstring, and the build-log entries for the full
story.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal, cast

from engine.options.selection import (
    ContractQuote,
    ContractSelectionInputs,
    select_contract,
)
from engine.options.sizing import OptionsSizingInputs, options_position_size
from engine.risk import RiskCaps
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
        system=DRAFTER, user=user, model=Model.SONNET, max_tokens=900,
        council_run_id=state.get("council_run_id"), user_id=state.get("user_id"),
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

    # `x or default` treats a GENUINELY-zero value the same as an absent
    # one (0.0 is falsy) — a fully-drawn-down account (equity=0.0) would
    # silently size a new trade against the fake $100k fixture instead of
    # refusing via atr_position_size's own equity<=0 zero-qty branch
    # (packages/engine/engine/sizing/atr.py). Only a missing key gets the
    # fixture; a present zero is a real fact about the account/quote and
    # must reach the sizer as zero.
    raw_last_price = ctx.get("last_price")
    last_price = float(raw_last_price) if raw_last_price is not None else 100.0
    raw_equity = ctx.get("portfolio_equity")
    equity = float(raw_equity) if raw_equity is not None else 100_000.0
    # An unparseable confidence collapses to 0.0, which sizes down to
    # qty<1 and converts the pass to HOLD below — the safe direction.
    confidence = clamp_confidence(data.get("confidence", 0.5), field="drafter.confidence")

    # Options Phase A branch — see the module docstring. Verdict is already
    # fixed to BUY/SELL/HOLD above (and HOLD already returned), so this can
    # only replace the SIZING path, never the verdict logic above it.
    if state.get("instrument") == "option":
        return await _draft_option_proposal(
            state,
            data,
            strategy_id=strategy_id,
            direction=direction,
            verdict=verdict,
            confidence=confidence,
            equity=equity,
        )

    atr_14 = ctx.get("technicals", {}).get("atr_14")

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


# ─────────────────────────────────────────────────────────────────────
# Options Phase A — contract selection + premium sizing.
# See the module docstring for why ``side`` is forced to "BUY" here and
# for the ``list_option_contracts`` dependency this is built against.
# ─────────────────────────────────────────────────────────────────────


async def _draft_option_proposal(
    state: CouncilState,
    data: dict[str, Any],
    *,
    strategy_id: str,
    direction: str,
    verdict: str,
    confidence: float,
    equity: float,
) -> CouncilState:
    """Contract selection + premium-at-risk sizing, in place of the ATR path.

    Never falls back to equity: no chain / no viable contract / a zeroed
    sizer are all named HOLDs, exactly mirroring the equity path's own
    qty<1 HOLD-conversion immediately above.
    """
    symbol = str(state["symbol"])
    ctx = state.get("context") or {}
    options_ctx = ctx.get("options_context") or {}
    raw_days_to_earnings = options_ctx.get("days_to_earnings")

    candidates = await _fetch_option_candidates(symbol)
    selection = select_contract(
        ContractSelectionInputs(
            underlying_symbol=symbol,
            direction=cast(Literal["long", "short"], direction if direction in ("long", "short") else "long"),
            conviction=confidence,
            candidates=candidates,
            now=datetime.now(UTC),
            days_to_earnings=int(raw_days_to_earnings) if raw_days_to_earnings is not None else None,
        )
    )

    if selection.selected is None:
        reason = selection.rejection_reason or "no_liquid_contract"
        logger.info(
            "options: no viable contract for %s (%s) — HOLD. funnel=%s",
            symbol, reason, selection.funnel_counts,
        )
        return {
            **state,
            "proposal": None,
            "final_action": "HOLD",
            "drafter_rationale": f"No liquid option contract found: {reason}",
            "bull_case": str(data.get("bull_case", "")).strip(),
            "bear_case": str(data.get("bear_case", "")).strip(),
        }

    leg = selection.selected
    caps = RiskCaps.from_env()
    budget_usd = equity * caps.options_max_premium_pct / 100.0
    sizing = options_position_size(
        OptionsSizingInputs(
            budget_usd=budget_usd,
            ask=float(leg.ask) if leg.ask is not None else 0.0,
            multiplier=leg.multiplier,
        )
    )

    if sizing.qty < 1:
        logger.info(
            "options: sizer returned qty=0 for %s — converting to HOLD (%s)",
            symbol, sizing.notes,
        )
        return {
            **state,
            "proposal": None,
            "final_action": "HOLD",
            "drafter_rationale": (
                f"{str(data.get('rationale', '')).strip()} | "
                f"Sizer returned 0 contracts: {sizing.notes}"
            ).strip(" |"),
            "bull_case": str(data.get("bull_case", "")).strip(),
            "bear_case": str(data.get("bear_case", "")).strip(),
        }

    rationale = str(data.get("rationale", "")).strip()
    sizer_note = f"Sizing (options_premium): {sizing.notes}"
    combined_rationale = f"{rationale} | {sizer_note}" if rationale else sizer_note
    ask = float(leg.ask) if leg.ask is not None else 0.0
    estimated_notional = round(sizing.qty * ask * leg.multiplier, 2)
    time_stop_days = _TIME_STOP_BY_HORIZON.get(str(state.get("horizon", "short")), 5)
    raw_last_price = ctx.get("last_price")
    underlying_last_price = float(raw_last_price) if raw_last_price is not None else None

    return {
        **state,
        "proposal": {
            "strategy": strategy_id,
            # Always "BUY" — Phase A only ever buys a call or a put to open
            # (never sells anything to open). See the module docstring:
            # this is load-bearing for engine.risk.rules._short.opens_short
            # never mistaking a bought PUT for a short position.
            "side": "BUY",
            "direction": direction,
            "opens_short": False,
            "qty": sizing.qty,
            # Always LIMIT, never MARKET — docs/OPTIONS_PLAN.md explicitly
            # recommends against market orders on a 15-min-delayed
            # indicative feed, and the executor forces this regardless of
            # what's written here. limit_price is NOT optional for an
            # options order: the broker layer builds a LimitOrderRequest
            # straight off this field with no None-guard (unlike its
            # STOP/STOP_LIMIT siblings, which do raise on a missing
            # price) — an unset limit_price here would reach Alpaca as a
            # limit order with no limit, which fails outright. `ask` is
            # the quoted premium this contract was selected/sized against.
            "order_type": "LIMIT",
            "limit_price": ask,
            "estimated_notional": estimated_notional,
            # Alpaca has no bracket for options (docs/OPTIONS_PLAN.md §3) —
            # populating either would promise an exit plan this order type
            # cannot keep. The expiry sweep + agent exit own the close.
            "stop_loss": None,
            "target_price": None,
            "time_stop_days": time_stop_days,
            "r_multiple": None,
            "rationale": combined_rationale,
            "bull_case": str(data.get("bull_case", "")),
            "bear_case": str(data.get("bear_case", "")),
            "risk_level": clamp_level(data.get("risk_level", 3), field="risk_level"),
            "conviction_level": clamp_level(
                data.get("conviction_level", 3), field="conviction_level"
            ),
            "confidence": confidence,
            "sizing_method": "options_premium",
            "sizing": {
                "method": "options_premium",
                "side": "BUY",
                "entry_price": ask,
                "underlying_last_price": underlying_last_price,
                "account_equity": equity,
                "confidence": confidence,
                "qty": sizing.qty,
                "budget_usd": round(budget_usd, 2),
                "premium_per_contract": round(ask * leg.multiplier, 2),
                "notional": estimated_notional,
                "pct_of_equity": round((estimated_notional / equity) * 100.0, 3) if equity > 0 else None,
                "r_multiple": None,
                "notes": sizing.notes,
            },
            # ── Option-snapshot fields ────────────────────────────────
            # Every field an options risk rule needs at execution time
            # (premium/ask, multiplier, strike, expiry, greeks/IV,
            # open_interest/volume for a fresh liquidity re-check) must be
            # written here NOW — the executor's re-risk-check reads the
            # PERSISTED proposal, not live state. Matches
            # ApprovalProposalDto's new fields, plus extra snapshot fields
            # (bid/ask/open_interest/volume/implied_volatility/
            # days_to_earnings) the task notes explicitly asked to err on
            # the side of including.
            "is_option": True,
            "option_action": leg.action,
            "occ_symbol": leg.occ_symbol,
            "strike": leg.strike,
            "expiry_date": leg.expiry,
            "contract_type": leg.contract_type,
            "multiplier": leg.multiplier,
            "underlying_symbol": leg.underlying_symbol,
            "open_interest": leg.open_interest,
            "volume": leg.volume,
            "bid": leg.bid,
            "ask": leg.ask,
            "implied_volatility": leg.implied_volatility,
            "days_to_earnings": leg.days_to_earnings,
        },
        # Mirrors the forced "BUY" side above — see module docstring.
        "final_action": "BUY",
    }


async def _fetch_option_candidates(symbol: str) -> tuple[ContractQuote, ...]:
    """Chain snapshot for ``symbol``, as ``ContractQuote`` candidates.

    Thin wrapper: the real fetch/merge/field-mapping lives in
    ``engine.options.contracts.fetch_option_candidates`` (a lazy import,
    mirroring ``engine.features.provider.AlpacaAssetInfoProvider.fetch``'s
    own lazy-import-a-broker-call convention — importing ``drafter.py``
    never fails because of it), independently unit-testable there without
    any LangGraph/LLM scaffolding. A missing/failing chain fetch degrades
    to zero candidates (which ``select_contract`` turns into a named
    ``no_candidates`` HOLD) rather than crashing the council run or
    silently falling back to equity sizing.
    """
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        return ()

    from engine.options.contracts import fetch_option_candidates

    try:
        return await fetch_option_candidates(
            symbol, api_key=api_key, secret_key=secret_key, now=datetime.now(UTC)
        )
    except Exception:
        logger.exception("options: chain fetch failed for %s", symbol)
        return ()
