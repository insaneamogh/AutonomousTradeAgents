"""Strategy-fit node — deterministic, LLM-free, and FIRST in the graph.

This is what replaced the Selector LLM node. It reads the feature dict,
asks every strategy to score its own preconditions
(``trading_agents.strategies.fit``), applies the Reflection loop's priors
as a bounded multiplier, and picks the winner in Python.

Two consequences, in order of how much they matter:

  1. **HOLD is free.** The node runs BEFORE the Router, so a symbol whose
     setup fits nothing costs zero LLM calls instead of five. This is the
     whole point: on a watchlist sweep, most symbols on most days are not
     setups, and the old graph paid full price to be told so.

  2. **The pick is auditable.** ``selector_rationale`` is now a named
     reason assembled from the component checks that carried the decision
     (``momentum_short:trailing_3m_return+risk_adjusted+trend_regime_aligned``)
     rather than a sentence a model wrote. Same string, strictly more
     information, and a test can assert on it.

**Why the Selector LLM node is gone rather than demoted to an explainer.**
A demoted node would spend a Haiku call to produce prose describing numbers
that are already in the audit row in structured form — decorative by
construction. Worse, it would reintroduce a failure mode on a path that
currently cannot fail: every LLM node in this council can degrade on a
parse error, and a degraded *explainer* would mark the whole run degraded
(excluding it from calibration) over cosmetics. The UI renders
``strategy_fit.components`` directly; that is a better explanation than a
paraphrase, and it costs nothing.

State contract (unchanged keys keep the DB columns and the Reflection loop
working):
    selected_strategy    strategy id, or None → HOLD
    selector_confidence  the prior-adjusted fit score, 0..1
    selector_rationale   the named reason
    selected_direction   "long" | "short"   (new)
    strategy_fit         full winner + ranking, for the audit row + UI (new)
    instrument           "option", additive — ONLY when ALLOW_OPTIONS AND
                         state["instrument_preference"] == "option" are
                         BOTH set and a strategy won. Absent otherwise,
                         which is every existing behavior, unchanged.

``selected_direction`` can be "short" on an options-eligible pass even when
``ALLOW_SHORTS`` is off: that flag gates the unbounded-loss EQUITY
short-selling machinery only, and a "short" direction reaching the options
fork only ever buys a PUT (bounded loss, Phase A in-scope — see
docs/OPTIONS_PLAYBOOK.md §1.2). It can never surface for an equity pass
(``instrument`` unset) unless ALLOW_SHORTS is genuinely on. See
``strategy_fit_node``'s ``options_eligible_pass`` for where this is decided.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.env import env_flag
from trading_agents.state import CouncilState
from trading_agents.strategies import best_strategy
from trading_agents.strategies.fit import _has_usable_features

logger = logging.getLogger("agents.node.strategy_fit")

MAX_RANKED_PERSISTED = 6
"""Ranked alternatives kept on the decision row. Enough to show "why this
one and not that one" in the UI without storing the full cross product."""


async def strategy_fit_node(state: CouncilState) -> CouncilState:
    """Pick the strategy + direction deterministically. No LLM, no I/O.

    Async only to match every other node's signature — the graph awaits
    them uniformly and a sync exception here would be the odd one out.
    """
    features = state.get("context") or {}
    priors = state.get("strategy_priors") or {}
    allow_shorts = env_flag("ALLOW_SHORTS")

    # An options-eligible pass may score (and win on) the SHORT direction
    # even when ALLOW_SHORTS is off. ALLOW_SHORTS exists to gate the
    # unbounded-loss EQUITY short-selling machinery
    # (engine.risk.rules.forbid_short_phase_0 and everything downstream of
    # it) — it has nothing to do with buying a PUT, which only ever
    # produces a bounded-loss BUY (drafter._draft_option_proposal and
    # options.agents both force side="BUY"/buy_to_open regardless of
    # direction — docs/OPTIONS_PLAYBOOK.md §1.2). Scoring only "long" here
    # regardless of instrument meant a cleanly bearish underlying —
    # precisely the case that makes the best PUT candidate — scored badly
    # on every strategy's LONG side, never reached MIN_FIT_TO_TRADE, and
    # the options Bull/Bear council (which can independently propose a
    # PUT; see trading_agents.options.agents / options.prompts.OPTIONS_BEAR)
    # never even ran for it. Both options consumers already handle a
    # "short" selected_direction correctly and are tested doing so
    # (test_options_drafter_bearish_thesis_buys_a_put_but_side_stays_buy in
    # test_options_drafter.py) — this was the one upstream gate stopping a
    # PUT from ever being tried. Computed BEFORE best_strategy() (not just
    # re-derived for the instrument-gate branch near the end of this
    # function) precisely so it can feed the scoring call below; the
    # bottom-of-function gate reuses this same variable, so a "short"
    # winner can only ever surface on a pass where `instrument` also ends
    # up set to "option".
    options_eligible_pass = (
        env_flag("ALLOW_OPTIONS") and state.get("instrument_preference") == "option"
    )
    score_shorts = allow_shorts or options_eligible_pass

    winner, ranked = best_strategy(
        features, priors=priors, allow_shorts=score_shorts
    )

    # Re-derived here (not threaded through `best_strategy`'s return value)
    # purely to explain a None winner — see that function's docstring. This
    # never changes the decision, only the rationale text and the audit
    # block below it.
    usable_features, unusable_reason = _has_usable_features(features)

    fit_block: dict[str, Any] = {
        "allow_shorts": allow_shorts,
        # Distinct from "allow_shorts" above: true only when the SHORT
        # side was scored because this pass is options-eligible, NOT
        # because ALLOW_SHORTS is on. Lets the audit trail tell "this pass
        # could have surfaced a PUT" apart from "this pass could have
        # opened an equity short" — the two read the same `winner.direction
        # == "short"` downstream but mean very different things.
        "options_may_score_short": options_eligible_pass and not allow_shorts,
        "winner": winner.as_dict() if winner else None,
        "ranked": [r.as_dict() for r in ranked[:MAX_RANKED_PERSISTED]],
        "priors_applied": dict(priors),
        "usable_features": usable_features,
    }
    if not usable_features:
        fit_block["unusable_reason"] = unusable_reason

    if winner is None:
        top = ranked[0] if ranked else None
        if not usable_features:
            # Distinct from the "genuinely marginal" branch below: `top`
            # (if any) may well show a score at or above MIN_FIT_TO_TRADE
            # here — that's the exact leak this branch exists to name
            # correctly rather than let read as "best was X at 0.60" while
            # returning HOLD, which looks like a bug rather than a gate.
            rationale = (
                f"Feature data too thin to trade ({unusable_reason}) — "
                "holding without spending an LLM call."
            )
        elif top:
            rationale = (
                f"No strategy clears the fit floor — best was {top.strategy_id} "
                f"({top.direction}) at {top.score:.2f}. Holding without spending an LLM call."
            )
        else:
            rationale = "No strategy could be scored (no usable features). Holding."
        logger.info(
            "strategy_fit: %s → HOLD before any LLM call (usable_features=%s best=%s %.3f)",
            state.get("symbol"),
            usable_features,
            top.strategy_id if top else "none",
            top.score if top else 0.0,
        )
        return {
            **state,
            "selected_strategy": None,
            "selected_direction": None,
            "selector_confidence": 0.0,
            "selector_rationale": rationale,
            "strategy_fit": fit_block,
            "proposal": None,
            "final_action": "HOLD",
        }

    logger.info(
        "strategy_fit: %s → %s %s (fit=%.3f prior=%.2f score=%.3f) %s",
        state.get("symbol"),
        winner.strategy_id,
        winner.direction,
        winner.fit,
        winner.prior,
        winner.score,
        winner.reason,
    )
    result: CouncilState = {
        **state,
        "selected_strategy": winner.strategy_id,
        "selected_direction": winner.direction,
        "selector_confidence": winner.score,
        "selector_rationale": winner.reason,
        "strategy_fit": fit_block,
    }

    # Options instrument gate — additive. Both the master ALLOW_OPTIONS env
    # switch (mirrors ALLOW_SHORTS above — same env_flag helper, same
    # fail-closed default) AND a per-run instrument preference must be
    # set, and a strategy must have actually won (we are past the `winner
    # is None` branch, so it has). When either condition is absent,
    # `result` is exactly what this node has always returned. Reuses
    # `options_eligible_pass` computed above the `best_strategy` call
    # rather than re-deriving it — same condition either way, which is
    # exactly what keeps a "short" winner from ever surfacing without
    # `instrument` also being set to "option" here.
    if options_eligible_pass:
        result["instrument"] = "option"

    return result
