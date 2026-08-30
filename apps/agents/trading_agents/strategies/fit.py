"""Deterministic strategy fit — each strategy scores its own preconditions.

This replaces the Selector LLM node. The old node handed a Haiku prompt a
set of English heuristics ("choppy → prefer mean-reversion") and asked it
to pick a strategy id. Every one of those heuristics is a threshold over
numbers we already computed deterministically upstream, so the LLM was
being paid to do arithmetic — and being paid *after* the three analyst
calls had already been spent.

Two things follow, and the second is the expensive one:

  1. **The pick belongs in Python.** ``agents propose, deterministic code
     disposes`` is the prime rule, and "which strategy is this setup" is a
     disposal question. A named reason (``trend_up_donchian_high``) is also
     strictly more auditable than a sentence of model prose, and it can be
     asserted on in a test.

  2. **The pick can run FIRST.** Preconditions read the feature dict, not
     the analysts, so nothing forces the fit computation to wait behind
     four LLM calls. A symbol that fits no strategy is HOLD, and it can be
     HOLD for **zero** LLM calls instead of five.

Scoring shape. Every strategy returns a set of named 0..1 ``FitComponent``
checks with weights; ``fit`` is their weighted mean. Components rather than
one opaque number because the UI has to explain *why* it fit, and because a
strategy that fits at 0.61 for one reason is a different animal from one
that fits at 0.61 for four.

Direction. Each strategy scores LONG and SHORT independently and keeps the
better side. That is what makes a bearish scanner trigger able to reach a
short proposal: the same Donchian logic that fires ``breakout`` at the top
of the channel fires it at the bottom, with ``direction="short"``.

Priors. The Reflection loop's per-strategy confidence multiplies the raw
fit — real learning from realized outcomes, and it must keep working. It
is applied as a bounded multiplier, never as an override: a prior cannot
promote a strategy whose preconditions are absent (0 x anything is 0), and
it cannot veto one whose preconditions are perfect (the multiplier floors
above zero). A learned prior should tilt a close call, not overrule the
tape.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Direction = Literal["long", "short"]

MIN_FIT_TO_TRADE = 0.42
"""Floor on the final (prior-adjusted) score. Below it the council HOLDs
without spending a single LLM call.

Why 0.45 and not 0.5 (the original reasoning, still why this lives in the
0.4-0.5 range at all): a component mean is a soft measure and the
strategies deliberately overlap, so a genuinely good setup rarely maxes
every check — a clean uptrend breakout scores ~0.75, and demanding 0.5+ on
every name would trade only the extremes. 0.45 is the level at which at
least half the preconditions are materially satisfied.

Lowered to 0.42 for the contest window (docs/PLAN_AGGRESSIVE_PROFILE.md
§2): opens the 0.42-0.45 band of marginal setups the council would
otherwise never see, on the same "maximize P&L on a paper account with a
fixed halt" reasoning behind ``RiskCaps.aggressive_paper()``.

**Hard floor: 0.41.** ``blind_weight_fraction("vol_regime_switch")`` is
exactly 0.400 — see ``test_blind_weight_stays_below_the_trade_floor``. At
or below 0.40 that strategy would clear the trade gate on direction-blind
checks alone (vol regime + ATR-not-stretched + idiosyncratic-vs-SPY — none
of which can tell long from short), which is exactly the failure that
measure exists to catch. 0.42 leaves margin above that floor; do not go
lower without re-deriving it.

It is a policy number, so it lives here in one reviewable place rather
than inline, and the cost/coverage trade-off it controls is measurable:
raise it and fewer symbols reach the council, lower it and more do.
"""

PRIOR_FLOOR = 0.6
PRIOR_CEILING = 1.15
"""Bounds on the Reflection-loop multiplier. A strategy the loop has soured
on is damped to 0.6x, a proven one lifted to 1.15x. Neither bound lets the
prior invent or destroy a setup — see the module docstring."""

NEUTRAL_PRIOR = 0.5
"""The confidence store's cold-start value. Maps to a 1.0x multiplier."""


@dataclass(frozen=True)
class FitComponent:
    """One named precondition check and what it saw.

    ``value`` is the raw feature that was tested, kept so the UI can show
    "RSI 24.1" next to "oversold" rather than only the verdict.
    """

    name: str
    score: float
    weight: float
    detail: str
    value: float | str | None = None
    directional: bool = True
    """Whether this check can tell LONG from SHORT.

    ``vol_regime_calm`` cannot: a 20%-vol name is calm whichever way you
    trade it. ``trend_regime_aligned`` can. The distinction is load-bearing
    — see ``blind_weight_fraction``, which pins the invariant that no
    strategy may clear the trade floor on direction-blind checks alone."""

    def as_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class StrategyFit:
    """One strategy's fit for one symbol, in one direction."""

    strategy_id: str
    direction: Direction
    fit: float
    """0..1 weighted mean of the components. Pre-prior."""
    prior: float
    """The Reflection-loop confidence for this strategy (0..1), or the
    neutral 0.5 when the loop has never graded it."""
    prior_multiplier: float
    score: float
    """``fit x prior_multiplier``, clamped to 0..1. What gets ranked."""
    reason: str
    """Named, greppable reason — the component names that carried the pick,
    joined. This is what lands in the audit row and the UI."""
    summary: str
    """One human sentence. Derived from the components, not written by a model."""
    components: tuple[FitComponent, ...] = field(default_factory=tuple)

    @property
    def tradable(self) -> bool:
        return self.score >= MIN_FIT_TO_TRADE

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "direction": self.direction,
            "fit": round(self.fit, 4),
            "prior": round(self.prior, 4),
            "prior_multiplier": round(self.prior_multiplier, 4),
            "score": round(self.score, 4),
            "reason": self.reason,
            "summary": self.summary,
            "tradable": self.tradable,
            "components": [c.as_dict() for c in self.components],
        }


# ─────────────────────────────────────────────────────────────────────
# Feature access — every read degrades to None, never raises
# ─────────────────────────────────────────────────────────────────────


class _Features:
    """Typed-ish accessor over the council's feature dict.

    The feature dict is assembled by several providers and any block can be
    absent (no fundamentals vendor, thin history, a failed optional fetch).
    A precondition that raises on a missing key would take the whole
    council down for a symbol with 59 bars, so every read here returns
    None and every check treats None as "cannot confirm" — which scores
    NEUTRAL, not zero. Absence of evidence is not evidence of a bad setup.
    """

    def __init__(self, features: Mapping[str, Any]) -> None:
        self._f = features
        self._tech = _block(features, "technicals")
        self._quant = _block(features, "quant")
        self._news = _block(features, "news")
        self._events = _block(features, "events")

    def tech(self, key: str) -> float | None:
        return _num(self._tech.get(key))

    def quant(self, key: str) -> float | None:
        return _num(self._quant.get(key))

    @property
    def trend_regime(self) -> str:
        return str(self._tech.get("trend_regime", "unknown"))

    @property
    def scan_directions(self) -> set[str]:
        """Directions the deterministic scanner flagged for this symbol."""
        triggers = self._f.get("scan_triggers") or []
        out: set[str] = set()
        for t in triggers:
            if isinstance(t, Mapping):
                d = str(t.get("direction", ""))
                if d in ("bullish", "bearish"):
                    out.add("long" if d == "bullish" else "short")
        return out

    @property
    def ex_dividend_in_horizon(self) -> bool:
        return bool(self._events.get("ex_dividend_in_horizon"))


def _block(features: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    b = features.get(key)
    return b if isinstance(b, Mapping) else {}


def _num(v: object) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


NEUTRAL = 0.5
"""Score for a check whose input is missing. Not 0: a strategy must not be
penalised for history the data provider could not supply."""


_EVIDENCE_QUANT_KEYS: tuple[str, ...] = (
    "price_zscore_20",
    "atr_zscore",
    "donchian_pct",
    "sharpe",
    "ret_21d_pct",
    "ret_63d_pct",
    "ret_252d_pct",
    "realized_vol_pct",
    "corr_benchmark",
)
"""The quant keys the five scorers actually read. Used ONLY by
``_has_usable_features`` below to count real evidence — not a scoring
input list, and not read by any ``FitComponent`` check itself."""


def _has_usable_features(features: Mapping[str, Any]) -> tuple[bool, str]:
    """(usable, why_not). An empty/near-empty feature dict must not be
    "tradable".

    Measured 2026-08-30 (docs/PLAN_AGGRESSIVE_PROFILE.md §0):
    ``best_strategy({})`` returns ``rsi_mean_reversion`` at 0.60 — not
    0.50 — because ``not_a_trend_break`` reads
    ``trend_regime != "downtrend"`` and the missing-value sentinel
    ``"unknown"`` satisfies that as a genuine TRUE, not NEUTRAL. Raising
    ``MIN_FIT_TO_TRADE`` does not fix this; only an explicit evidence gate
    does.

    Called from ``best_strategy`` ONLY. ``score_strategy``/
    ``rank_strategies`` (and, through them, ``blind_weight_fraction``,
    which calls the raw per-strategy scorer directly on
    ``_Features({})``) must keep scoring a thin or empty dict exactly as
    they always have — every component degrades to NEUTRAL on a missing
    input, per this module's own "absence of evidence is not evidence of
    a bad setup" convention. Only the outermost "is this tradable"
    decision point gets to refuse on thin evidence.
    """
    tech = _block(features, "technicals")
    if not tech:
        return False, "no technicals reported"

    trend_regime = str(tech.get("trend_regime", "unknown"))
    if trend_regime in ("", "unknown"):
        return False, f"trend_regime is {trend_regime!r}"

    quant = _block(features, "quant")
    present = sum(1 for k in _EVIDENCE_QUANT_KEYS if _num(quant.get(k)) is not None)
    if present < 3:
        return False, (
            f"only {present}/{len(_EVIDENCE_QUANT_KEYS)} quant signals present (need >= 3)"
        )

    return True, ""


def _ramp(value: float | None, *, low: float, high: float) -> float:
    """Linear 0→1 ramp between ``low`` and ``high``. Missing → NEUTRAL.

    ``low > high`` inverts it, which is how "lower is better" checks are
    written without a second helper.
    """
    if value is None:
        return NEUTRAL
    if low == high:
        return 1.0 if value >= low else 0.0
    t = (value - low) / (high - low)
    return max(0.0, min(1.0, t))


def _band(value: float | None, *, lo: float, hi: float, soft: float) -> float:
    """1.0 inside [lo, hi], decaying to 0 over ``soft`` units outside it."""
    if value is None:
        return NEUTRAL
    if lo <= value <= hi:
        return 1.0
    distance = lo - value if value < lo else value - hi
    return max(0.0, 1.0 - distance / soft)


def _flag(condition: bool | None) -> float:
    if condition is None:
        return NEUTRAL
    return 1.0 if condition else 0.0


# ─────────────────────────────────────────────────────────────────────
# The five strategies. One scorer each.
#
# Each returns the components for ONE direction. The shared runner scores
# both directions and keeps the better.
# ─────────────────────────────────────────────────────────────────────


def _sma_crossover(f: _Features, d: Direction) -> list[FitComponent]:
    """Trend-follower: price on the right side of both moving averages,
    with the averages themselves stacked the right way.

    Loses in chop, so ``trend_regime`` is the heaviest single check.
    """
    long = d == "long"
    want = "uptrend" if long else "downtrend"
    dma20, dma50 = f.tech("dma20_pct"), f.tech("dma50_pct")
    sign = 1.0 if long else -1.0
    return [
        FitComponent(
            "trend_regime_aligned",
            _flag(f.trend_regime == want) if f.trend_regime != "unknown" else NEUTRAL,
            0.35,
            f"trend_regime={f.trend_regime}, wants {want}",
            f.trend_regime,
        ),
        FitComponent(
            "price_vs_20dma",
            _ramp(sign * dma20 if dma20 is not None else None, low=-1.0, high=3.0),
            0.25,
            f"{'above' if long else 'below'} the 20-DMA by {dma20:.2f}%" if dma20 is not None else "20-DMA distance unavailable",
            dma20,
        ),
        FitComponent(
            "price_vs_50dma",
            _ramp(sign * dma50 if dma50 is not None else None, low=-1.0, high=5.0),
            0.25,
            f"{'above' if long else 'below'} the 50-DMA by {dma50:.2f}%" if dma50 is not None else "50-DMA distance unavailable",
            dma50,
        ),
        FitComponent(
            "not_overextended",
            _band(f.quant("price_zscore_20"), lo=-2.0, hi=2.0, soft=1.5),
            0.15,
            "price within 2 sigma of its 20-day mean — a cross, not a blow-off",
            f.quant("price_zscore_20"),
            directional=False,
        ),
    ]


def _rsi_mean_reversion(f: _Features, d: Direction) -> list[FitComponent]:
    """Counter-trend: buy oversold / short overbought, and ONLY when the
    stretch is real in the name's own units.

    ``price_zscore_20`` carries more weight than RSI because RSI 28 means
    something different on SPY than on a 6%-ATR small cap, and the z-score
    is the version of that question that is comparable across names.
    """
    long = d == "long"
    rsi = f.tech("rsi_14")
    z = f.quant("price_zscore_20")
    signed_z = -z if (z is not None and long) else z
    return [
        FitComponent(
            "rsi_extreme",
            _ramp(40.0 - rsi if (rsi is not None and long) else (rsi - 60.0 if rsi is not None else None), low=0.0, high=15.0),
            0.3,
            f"RSI-14 at {rsi:.1f} ({'oversold' if long else 'overbought'} side)" if rsi is not None else "RSI unavailable",
            rsi,
        ),
        FitComponent(
            "zscore_stretch",
            _ramp(signed_z, low=1.0, high=2.5),
            0.35,
            f"price {abs(z):.2f} sigma {'below' if (z or 0) < 0 else 'above'} its 20-day mean" if z is not None else "z-score unavailable",
            z,
        ),
        FitComponent(
            "not_a_trend_break",
            _flag(f.trend_regime != ("downtrend" if long else "uptrend")),
            0.2,
            "regime is not fighting the snap-back — mean-reversion into a "
            "live trend is just catching a falling knife",
            f.trend_regime,
        ),
        FitComponent(
            "vol_not_exploding",
            _ramp(f.quant("atr_zscore"), low=2.0, high=0.0),
            0.15,
            "ATR has not blown out; a vol explosion means the old mean is gone",
            f.quant("atr_zscore"),
            directional=False,
        ),
    ]


def _momentum(f: _Features, d: Direction) -> list[FitComponent]:
    """12-1 momentum: the medium-horizon return stream, gated on it being
    worth the risk it took (Sharpe) rather than just large."""
    long = d == "long"
    sign = 1.0 if long else -1.0
    r252, r63 = f.quant("ret_252d_pct"), f.quant("ret_63d_pct")
    sharpe = f.quant("sharpe")
    return [
        FitComponent(
            "trailing_12m_return",
            _ramp(sign * r252 if r252 is not None else None, low=0.0, high=25.0),
            0.3,
            f"252-day return {r252:.1f}%" if r252 is not None else "12m return unavailable",
            r252,
        ),
        FitComponent(
            "trailing_3m_return",
            _ramp(sign * r63 if r63 is not None else None, low=-2.0, high=12.0),
            0.25,
            f"63-day return {r63:.1f}%" if r63 is not None else "3m return unavailable",
            r63,
        ),
        FitComponent(
            "risk_adjusted",
            _ramp(sign * sharpe if sharpe is not None else None, low=-0.3, high=1.2),
            0.25,
            f"Sharpe {sharpe:.2f} over the lookback — momentum that paid for its vol" if sharpe is not None else "Sharpe unavailable",
            sharpe,
        ),
        FitComponent(
            "trend_regime_aligned",
            _flag(f.trend_regime == ("uptrend" if long else "downtrend")) if f.trend_regime != "unknown" else NEUTRAL,
            0.2,
            f"trend_regime={f.trend_regime}",
            f.trend_regime,
        ),
    ]


def _breakout(f: _Features, d: Direction) -> list[FitComponent]:
    """Donchian: price at the edge of its own 20-day channel, confirmed by
    participation. A breakout on no volume is a drift, not a break."""
    long = d == "long"
    don = f.quant("donchian_pct")
    positioned = don if long else (100.0 - don if don is not None else None)
    vol_ratio = f.tech("volume_ratio_20d")
    return [
        FitComponent(
            "donchian_edge",
            _ramp(positioned, low=70.0, high=95.0),
            0.45,
            f"{don:.0f}% up the 20-day channel ({'upper' if long else 'lower'} edge is the setup)" if don is not None else "channel position unavailable",
            don,
        ),
        FitComponent(
            "volume_confirms",
            _ramp(vol_ratio, low=0.9, high=1.8),
            0.2,
            f"volume {vol_ratio:.2f}x its 20-day average" if vol_ratio is not None else "volume ratio unavailable",
            vol_ratio,
            directional=False,
        ),
        FitComponent(
            "range_expanding",
            _ramp(f.quant("atr_zscore"), low=-0.5, high=1.5),
            0.15,
            "ATR expanding — breakouts happen on widening ranges, not quiet ones",
            f.quant("atr_zscore"),
            directional=False,
        ),
        FitComponent(
            "scanner_agrees",
            1.0 if d in f.scan_directions else (NEUTRAL if not f.scan_directions else 0.25),
            0.2,
            f"scanner directions: {sorted(f.scan_directions) or 'none this pass'}",
            ",".join(sorted(f.scan_directions)) or None,
        ),
    ]


def _vol_regime_switch(f: _Features, d: Direction) -> list[FitComponent]:
    """Momentum, but it sits out high vol. The distinguishing check is the
    vol REGIME, so that is where the weight goes — this strategy's whole
    claim is knowing when not to trade."""
    long = d == "long"
    sign = 1.0 if long else -1.0
    rvol = f.quant("realized_vol_pct")
    r21 = f.quant("ret_21d_pct")
    return [
        FitComponent(
            "momentum_present",
            _ramp(sign * r21 if r21 is not None else None, low=-1.0, high=8.0),
            0.35,
            f"21-day return {r21:.1f}%" if r21 is not None else "21d return unavailable",
            r21,
        ),
        FitComponent(
            "trend_regime_aligned",
            _flag(f.trend_regime == ("uptrend" if long else "downtrend")) if f.trend_regime != "unknown" else NEUTRAL,
            0.25,
            f"trend_regime={f.trend_regime}",
            f.trend_regime,
        ),
        FitComponent(
            "vol_regime_calm",
            _ramp(rvol, low=55.0, high=20.0),
            0.25,
            f"realized vol {rvol:.1f}% annualized — the regime this strategy will trade" if rvol is not None else "realized vol unavailable",
            rvol,
            directional=False,
        ),
        FitComponent(
            "atr_not_stretched",
            _ramp(f.quant("atr_zscore"), low=1.5, high=-0.5),
            0.1,
            "ATR near or below its own norm",
            f.quant("atr_zscore"),
            directional=False,
        ),
        FitComponent(
            "idiosyncratic",
            _ramp(f.quant("corr_benchmark"), low=0.95, high=0.3),
            0.05,
            "correlation to SPY low enough that this is the name, not the index",
            f.quant("corr_benchmark"),
            directional=False,
        ),
    ]


_SCORERS = {
    "sma_crossover": _sma_crossover,
    "rsi_mean_reversion": _rsi_mean_reversion,
    "momentum": _momentum,
    "breakout": _breakout,
    "vol_regime_switch": _vol_regime_switch,
}


# ─────────────────────────────────────────────────────────────────────
# The runner
# ─────────────────────────────────────────────────────────────────────


def prior_multiplier(prior: float | None) -> float:
    """Map a 0..1 Reflection confidence onto [PRIOR_FLOOR, PRIOR_CEILING].

    ``NEUTRAL_PRIOR`` (0.5, the cold-start value) maps to exactly 1.0, so a
    strategy the loop has never graded is neither helped nor hurt.
    """
    p = NEUTRAL_PRIOR if prior is None else max(0.0, min(1.0, prior))
    if p >= NEUTRAL_PRIOR:
        span = (p - NEUTRAL_PRIOR) / (1.0 - NEUTRAL_PRIOR)
        return 1.0 + span * (PRIOR_CEILING - 1.0)
    span = (NEUTRAL_PRIOR - p) / NEUTRAL_PRIOR
    return 1.0 - span * (1.0 - PRIOR_FLOOR)


def blind_weight_fraction(strategy_id: str) -> float:
    """Share of a strategy's weight that cannot distinguish long from short.

    This is the number that decides whether a strategy can score a
    confident SHORT on a name in a clean uptrend. If the direction-blind
    checks alone carry more weight than ``MIN_FIT_TO_TRADE``, then a
    perfect score on them clears the floor with every directional check at
    zero — which is exactly the bug this measure exists to prevent. The
    test suite asserts it stays below the floor for all five strategies.

    Measured on a neutral feature dict so the weights, not the data, are
    what is being read.
    """
    components = _SCORERS[strategy_id](_Features({}), "long")
    total = sum(c.weight for c in components)
    if total <= 0:
        return 0.0
    return sum(c.weight for c in components if not c.directional) / total


def _weighted(components: list[FitComponent]) -> float:
    total = sum(c.weight for c in components)
    if total <= 0:
        return 0.0
    return sum(c.score * c.weight for c in components) / total


def _reason_and_summary(
    strategy_id: str, direction: Direction, components: list[FitComponent]
) -> tuple[str, str]:
    """Name the pick after the checks that actually carried it.

    "Carried it" = scored above 0.6, ordered by weighted contribution. When
    nothing cleared 0.6 the reason says so explicitly — a marginal pick
    should read as marginal in the audit log, not be dressed up by naming
    its least-bad component.
    """
    strong = sorted(
        (c for c in components if c.score >= 0.6),
        key=lambda c: c.score * c.weight,
        reverse=True,
    )
    if not strong:
        return (
            f"{strategy_id}_{direction}_marginal",
            f"No precondition of {strategy_id} is clearly satisfied; the fit is marginal.",
        )
    names = "+".join(c.name for c in strong[:3])
    reason = f"{strategy_id}_{direction}:{names}"
    summary = "; ".join(c.detail for c in strong[:3])
    return reason, summary


def score_strategy(
    strategy_id: str,
    features: Mapping[str, Any],
    *,
    direction: Direction,
    priors: Mapping[str, float] | None = None,
) -> StrategyFit:
    """Score one strategy in one direction. Pure function of the feature dict."""
    scorer = _SCORERS[strategy_id]
    f = _Features(features)
    components = scorer(f, direction)
    fit = _weighted(components)
    prior = (priors or {}).get(strategy_id, NEUTRAL_PRIOR)
    mult = prior_multiplier(prior)
    reason, summary = _reason_and_summary(strategy_id, direction, components)
    return StrategyFit(
        strategy_id=strategy_id,
        direction=direction,
        fit=round(fit, 4),
        prior=prior,
        prior_multiplier=round(mult, 4),
        score=round(max(0.0, min(1.0, fit * mult)), 4),
        reason=reason,
        summary=summary,
        components=tuple(components),
    )


def rank_strategies(
    features: Mapping[str, Any],
    *,
    priors: Mapping[str, float] | None = None,
    allow_shorts: bool = False,
) -> list[StrategyFit]:
    """Every strategy x every allowed direction, best score first.

    ``allow_shorts=False`` (the default, matching ``ALLOW_SHORTS``) does not
    merely filter the short results out at the end — it never scores them.
    A short that the risk engine would categorically veto has no business
    winning the ranking and then being thrown away, because the symbol
    would look "picked" in the audit log while producing nothing.
    """
    directions: tuple[Direction, ...] = ("long", "short") if allow_shorts else ("long",)
    out = [
        score_strategy(sid, features, direction=d, priors=priors)
        for sid in _SCORERS
        for d in directions
    ]
    # Sort by score, then by fit (so a raw-fit tie isn't decided by prior
    # alone), then by id for a stable, reproducible order.
    out.sort(key=lambda s: (-s.score, -s.fit, s.strategy_id))
    return out


def best_strategy(
    features: Mapping[str, Any],
    *,
    priors: Mapping[str, float] | None = None,
    allow_shorts: bool = False,
) -> tuple[StrategyFit | None, list[StrategyFit]]:
    """``(winner_or_None, full_ranking)``.

    ``None`` means either nothing cleared ``MIN_FIT_TO_TRADE``, or the
    feature dict itself is too thin to call anything "tradable" at all —
    see ``_has_usable_features``. Either way the caller HOLDs, and does so
    before spending anything on an LLM. ``ranked`` is always the real
    ranking (even on the evidence-gate path) so the audit row can still
    show what the nominal winner would have been — see
    ``trading_agents.nodes.strategy_fit``, which calls
    ``_has_usable_features`` again to tell the two HOLD reasons apart.
    """
    ranked = rank_strategies(features, priors=priors, allow_shorts=allow_shorts)
    usable, _why_not = _has_usable_features(features)
    if not usable or not ranked or not ranked[0].tradable:
        return None, ranked
    return ranked[0], ranked
