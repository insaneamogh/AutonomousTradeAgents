"""`strategy_fit` as an `AlphaModel` — the first implementation of the seam.

An adapter, not a rewrite. `best_strategy` is untouched and keeps deciding
exactly what it decided before; this only restates its answer in the shape
every candidate must produce, so the incumbent and any challenger clear the
same harness.

The one judgement call is which of `best_strategy`'s two `None` cases is an
abstention:

    features too thin  -> ABSTAIN. We never formed a view. Scoring this as
                          a flat call would count a data gap as an opinion.
    nothing cleared    -> FLAT (value 0.0). We looked and nothing qualified.
       MIN_FIT_TO_TRADE  That IS a view, and a backtest should score it.

`best_strategy` already separates these (`_has_usable_features`), and the
council relies on the distinction to tell its two HOLD reasons apart. This
preserves it rather than flattening both to "no signal" — which is the
whole reason `Signal.abstained` exists.
"""

from __future__ import annotations

from typing import Any

from engine.alpha import Signal
from trading_agents.strategies.fit import (
    MIN_FIT_TO_TRADE,
    _has_usable_features,
    best_strategy,
)


class StrategyFitAlpha:
    """The shipped signal, behind the seam.

    Rank key is `conviction`, not `score`: `score` is a weighted mean of ~9
    bounded components and measurably compresses — 300 symbols all landed
    inside a 0.3% band — so it cannot carry magnitude. `conviction` is a
    tail statistic over the same components and does separate cases. See
    `StrategyFit.conviction`.
    """

    def __init__(self, *, allow_shorts: bool = True) -> None:
        self._allow_shorts = allow_shorts

    @property
    def name(self) -> str:
        return "strategy_fit"

    def evaluate(self, snapshot: dict[str, Any], *, symbol: str) -> Signal:
        usable, why_not = _has_usable_features(snapshot)
        if not usable:
            return Signal.abstain(self.name, symbol, reason=why_not or "thin_features")

        fit, ranked = best_strategy(snapshot, allow_shorts=self._allow_shorts)
        if fit is None:
            # A real observation: the evidence was readable and nothing in it
            # cleared the floor. Scored, not discarded.
            top = ranked[0].score if ranked else 0.0
            return Signal(
                name=self.name, symbol=symbol, value=0.0,
                reason=f"no_strategy_cleared_{MIN_FIT_TO_TRADE:.2f}",
                meta={"top_score": top},
            )

        sign = 1.0 if fit.direction == "long" else -1.0
        return Signal(
            name=self.name,
            symbol=symbol,
            value=sign * min(1.0, max(0.0, fit.conviction)),
            reason=fit.reason,
            meta={
                "strategy_id": fit.strategy_id,
                # Recorded even though `value`'s sign already encodes it.
                # `conviction` is legitimately 0.0 for a strategy that
                # cleared the floor without any component STRONGLY agreeing
                # — so `value` is 0.0, `direction` reads "flat", and a real
                # directional call becomes indistinguishable from no call.
                # That is the cost of packing direction and magnitude into
                # one number. Consumers that need "was a call made?" must
                # test `strategy_id`, NOT `value != 0`.
                "direction": fit.direction,
                "score": fit.score,
                "conviction": fit.conviction,
                "fit": fit.fit,
                "prior": fit.prior,
            },
        )
