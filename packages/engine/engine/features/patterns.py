"""Candlestick pattern recognition over daily OHLCV bars — pure, deterministic math.

Same policy as ``quant.py`` and ``technicals.py``: stdlib ``math`` and list
comprehensions only. **``pandas``, ``numpy`` and ``pandas-ta`` are forbidden
here and in this module's tests** — ``pandas-ta`` ships a ``cdl_pattern``
helper that looks tempting, but importing it emits a deprecation warning
under this repo's pandas/numpy pins, and ``pytest`` runs with
``filterwarnings = ["error"]``, so that import would fail the entire suite
at collection, not just a test in this file. See ``docs/PLAN_CANDLE_PATTERNS.md``
§0.

``atr`` and ``trend_regime`` are parameters, never recomputed — ``compute_technicals``
already produces both, and duplicating that math here would be exactly the
"same number in two places" trap CLAUDE.md §4.4 exists because of.

Scoring shape, every pattern, no exceptions: ``quality x magnitude x context``,
each in 0..1:

  - **quality** — how cleanly the geometry holds, as a ramp (``_ramp``), never
    a threshold. A marginal pattern scores marginally instead of flipping a
    switch.
  - **magnitude** — was the move big enough, in this name's OWN volatility
    units (ATR), to mean anything. ``_magnitude`` ramps a bar's range from
    half an ATR (score ~0) to 1.5 ATR (score 1.0) — a technically perfect
    hammer on a 0.1%-range day is noise wearing a costume, regardless of how
    clean its geometry is. This is the load-bearing part of the whole design.

    **Compression is the one deliberate inversion.** ``inside_bar``/``nr7``
    are coils — their entire claim is that the range is SMALL, so rewarding a
    big range would contradict the pattern's own definition. ``_coil_magnitude``
    ramps the other way (small range -> high score), still against the name's
    own ATR so a "narrowest of 7" during an already-loud, high-vol week
    doesn't read as a meaningful coil merely for being the least-loud of a
    loud crowd.
  - **context** — the trend gate. A reversal pattern scores 1.0 in the
    counter-trend regime it belongs to (a hammer wants a downtrend), 0.4 in
    choppy/unknown (no penalty for an unreadable tape), 0.15 in the regime it
    contradicts. A continuation pattern scores 1.0 aligned, 0.3 otherwise.
    ``indecision``/``compression``/``expansion`` skip this factor entirely
    (equivalent to a fixed 1.0) — they are direction-neutral by definition.

Every pattern's raw score feeds exactly one of the seven ``PatternBlock``
fields, and family aggregation is **max, never sum** — summing lets three
weak patterns outscore one clean one, which is backwards. ``names`` lists
every INDIVIDUAL pattern (not the aggregated family) that scored >= 0.35,
strongest first; ``top_pattern``/``top_pattern_score`` name the single
strongest one.

Guards: ``atr <= 0`` or fewer than ``MIN_BARS_FOR_PATTERNS`` bars returns the
all-zero block with ``names=()``. **Never raises** — a pattern failure must
not take down the whole feature pass for one symbol.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from engine.features.technicals import DailyBar

#: nr7 needs a trailing 7-bar window to ask "is this the narrowest of the
#: last 7" — every other pattern here needs at most 3 bars, so 7 is the
#: binding minimum for the whole module, not just nr7's own guard.
MIN_BARS_FOR_PATTERNS = 7

#: Guards a bare division by an exactly-zero body/range without distorting
#: any real ratio — every value this module divides by is an ATR-unit
#: quantity, always well above 1e-9 when it is genuinely nonzero.
_EPS = 1e-9

#: A pattern must clear this score to appear in ``names``. Plan §1.
_NAME_THRESHOLD = 0.35

#: Magnitude ramp bounds for every family EXCEPT compression: below half an
#: ATR of range the move scores ~0 no matter how clean the geometry; at or
#: above 1.5 ATR it scores the full 1.0.
_MAG_LOW, _MAG_HIGH = 0.5, 1.5

#: Compression's inverted magnitude bounds — see the module docstring.
_COIL_LOW, _COIL_HIGH = 1.3, 0.35


@dataclass(frozen=True)
class PatternBlock:
    """Deterministic candlestick-pattern block for one symbol's latest bars.

    Mirrors ``QuantFeatures``'s shape: every field is always populated
    (never ``None``) because ``detect_patterns`` never raises and never
    partially degrades — the all-zero block IS the "nothing detected"
    answer.
    """

    reversal_bull: float
    """Strongest bullish-reversal pattern this pass: hammer, bullish
    engulfing, bullish harami, piercing line, morning star."""
    reversal_bear: float
    """Strongest bearish-reversal pattern: shooting star, bearish engulfing,
    bearish harami, dark cloud cover, evening star."""
    continuation_bull: float
    """Strongest bullish-continuation pattern: bull marubozu, three white
    soldiers."""
    continuation_bear: float
    """Strongest bearish-continuation pattern: bear marubozu, three black
    crows."""
    indecision: float
    """Doji family. Direction-neutral by definition."""
    compression: float
    """Inside bar / NR7 — a coil. Direction-neutral."""
    expansion: float
    """Outside bar / wide-range. Direction-neutral."""
    names: tuple[str, ...]
    """Every individual pattern scoring >= 0.35 this pass, strongest first."""
    top_pattern: str | None
    """The single strongest individual pattern, or None if nothing cleared
    the naming threshold."""
    top_pattern_score: float
    """That pattern's raw score, or 0.0 when ``top_pattern`` is None."""

    def as_dict(self) -> dict[str, Any]:
        """Prompt/JSON-friendly view. Same keys as the dataclass fields."""
        return dict(asdict(self))


def _empty_block() -> PatternBlock:
    """All-zero block — returned on the guard path. Never ``None`` fields:
    a pattern block always has this exact shape, degraded or not."""
    return PatternBlock(
        reversal_bull=0.0,
        reversal_bear=0.0,
        continuation_bull=0.0,
        continuation_bear=0.0,
        indecision=0.0,
        compression=0.0,
        expansion=0.0,
        names=(),
        top_pattern=None,
        top_pattern_score=0.0,
    )


def detect_patterns(bars: list[DailyBar], *, atr: float, trend_regime: str) -> PatternBlock:
    """The council's ``patterns`` feature block from daily bars.

    ``bars`` must be oldest -> newest (matches ``compute_technicals``).
    ``atr`` is the symbol's current ATR-14 (same units as the bars' prices);
    ``trend_regime`` is ``compute_technicals``'s own classification
    (``"uptrend"``/``"downtrend"``/``"choppy"``) — both parameters, never
    recomputed. Never raises: a pattern failure must not take down the whole
    feature pass for one symbol.
    """
    if atr <= 0 or len(bars) < MIN_BARS_FOR_PATTERNS:
        return _empty_block()

    scores: dict[str, float] = {
        "hammer": _hammer(bars, atr, trend_regime),
        "shooting_star": _shooting_star(bars, atr, trend_regime),
        "doji": _doji(bars, atr),
        "marubozu_bull": _marubozu_bull(bars, atr, trend_regime),
        "marubozu_bear": _marubozu_bear(bars, atr, trend_regime),
        "bullish_engulfing": _bullish_engulfing(bars, atr, trend_regime),
        "bearish_engulfing": _bearish_engulfing(bars, atr, trend_regime),
        "bullish_harami": _bullish_harami(bars, atr, trend_regime),
        "bearish_harami": _bearish_harami(bars, atr, trend_regime),
        "piercing_line": _piercing_line(bars, atr, trend_regime),
        "dark_cloud_cover": _dark_cloud_cover(bars, atr, trend_regime),
        "morning_star": _morning_star(bars, atr, trend_regime),
        "evening_star": _evening_star(bars, atr, trend_regime),
        "three_white_soldiers": _three_white_soldiers(bars, atr, trend_regime),
        "three_black_crows": _three_black_crows(bars, atr, trend_regime),
        "inside_bar": _inside_bar(bars, atr),
        "outside_bar": _outside_bar(bars, atr),
        "nr7": _nr7(bars, atr),
    }

    named = sorted(
        (name for name, score in scores.items() if score >= _NAME_THRESHOLD),
        key=lambda name: -scores[name],
    )
    names = tuple(named)
    top_pattern = names[0] if names else None
    top_pattern_score = scores[top_pattern] if top_pattern is not None else 0.0

    return PatternBlock(
        reversal_bull=max(
            scores["hammer"],
            scores["bullish_engulfing"],
            scores["bullish_harami"],
            scores["piercing_line"],
            scores["morning_star"],
        ),
        reversal_bear=max(
            scores["shooting_star"],
            scores["bearish_engulfing"],
            scores["bearish_harami"],
            scores["dark_cloud_cover"],
            scores["evening_star"],
        ),
        continuation_bull=max(scores["marubozu_bull"], scores["three_white_soldiers"]),
        continuation_bear=max(scores["marubozu_bear"], scores["three_black_crows"]),
        indecision=scores["doji"],
        compression=max(scores["inside_bar"], scores["nr7"]),
        expansion=scores["outside_bar"],
        names=names,
        top_pattern=top_pattern,
        top_pattern_score=top_pattern_score,
    )


# ─────────────────────────────────────────────────────────────────────
# Shared primitives
# ─────────────────────────────────────────────────────────────────────


def _ramp(value: float, *, low: float, high: float) -> float:
    """Linear 0->1 ramp between ``low`` and ``high``, clamped.

    ``low > high`` inverts the ramp — every "smaller/lower is better" check
    (a small upper wick, a small body, a tight coil) uses this rather than a
    second helper.
    """
    if low == high:
        return 1.0 if value >= low else 0.0
    t = (value - low) / (high - low)
    return max(0.0, min(1.0, t))


def _primitives(bar: DailyBar) -> tuple[float, float, float, float]:
    """(body, upper_wick, lower_wick, range), all >= 0, in price units."""
    body = abs(bar.close - bar.open)
    upper = bar.high - max(bar.open, bar.close)
    lower = min(bar.open, bar.close) - bar.low
    rng = bar.high - bar.low
    return body, upper, lower, rng


def _body_extremes(bar: DailyBar) -> tuple[float, float]:
    """(low, high) of the candle BODY — open/close, not the wick range."""
    return (bar.open, bar.close) if bar.open <= bar.close else (bar.close, bar.open)


def _is_bullish(bar: DailyBar) -> bool:
    return bar.close > bar.open


def _is_bearish(bar: DailyBar) -> bool:
    return bar.close < bar.open


def _body_over_range_ramp(bar: DailyBar, *, low: float, high: float) -> float:
    body, _upper, _lower, rng = _primitives(bar)
    return _ramp(body / max(rng, _EPS), low=low, high=high)


def _magnitude(rng: float, atr: float) -> float:
    """Shared 'was the move big enough to mean anything' ramp — every
    family except compression. See the module docstring."""
    return _ramp(rng / atr, low=_MAG_LOW, high=_MAG_HIGH)


def _coil_magnitude(rng: float, atr: float) -> float:
    """Compression's inverted magnitude — smaller range scores higher, still
    against this name's own ATR. See the module docstring."""
    return _ramp(rng / atr, low=_COIL_LOW, high=_COIL_HIGH)


def _reversal_context(trend_regime: str, belongs_to: str) -> float:
    """1.0 in the regime this reversal belongs to, 0.4 choppy/unknown, 0.15
    in the regime it contradicts."""
    if trend_regime == belongs_to:
        return 1.0
    if trend_regime in ("choppy", "unknown", ""):
        return 0.4
    return 0.15


def _continuation_context(trend_regime: str, aligned: str) -> float:
    """1.0 aligned with the trend this continuation extends, 0.3 otherwise."""
    return 1.0 if trend_regime == aligned else 0.3


def _engulf_quality(prior: DailyBar, cur: DailyBar, atr: float) -> float:
    """How cleanly ``cur``'s body contains ``prior``'s body, ATR-normalised.
    Shared by bullish/bearish engulfing — the containment math is identical;
    only the color gate and the family it feeds differ."""
    prior_lo, prior_hi = _body_extremes(prior)
    cur_lo, cur_hi = _body_extremes(cur)
    lower_margin = prior_lo - cur_lo
    upper_margin = cur_hi - prior_hi
    return _ramp(min(lower_margin, upper_margin) / atr, low=-0.1, high=0.3)


def _penetration_ramp(penetration: float) -> float:
    """Shared piercing-line / dark-cloud-cover quality: rewards a close
    50%-90% into the prior body, then FADES back out past a full
    retracement (penetration >= 1.0) — that region is a bullish/bearish
    engulfing's territory, not a piercing line's. Without the fade, a clean
    engulfing fixture also reads as a maximal piercing line, which would
    make ``reversal_bull``/``reversal_bear`` name the wrong pattern."""
    return _ramp(penetration, low=0.5, high=0.9) * _ramp(penetration, low=1.3, high=1.0)


def _harami_quality(prior: DailyBar, cur: DailyBar, atr: float) -> float:
    """How cleanly ``cur``'s (smaller) body sits inside ``prior``'s body —
    containment the other way from engulfing, plus a size-contrast check so
    a same-size body that happens to nest isn't scored as a harami."""
    prior_lo, prior_hi = _body_extremes(prior)
    cur_lo, cur_hi = _body_extremes(cur)
    lower_margin = cur_lo - prior_lo
    upper_margin = prior_hi - cur_hi
    containment = _ramp(min(lower_margin, upper_margin) / atr, low=-0.05, high=0.15)
    prior_body = prior_hi - prior_lo
    cur_body = cur_hi - cur_lo
    size_contrast = _ramp(cur_body / max(prior_body, _EPS), low=0.7, high=0.2)
    return containment * size_contrast


# ─────────────────────────────────────────────────────────────────────
# Single-bar patterns
# ─────────────────────────────────────────────────────────────────────


def _hammer(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Small body, a long lower wick, minimal upper wick. Bullish reversal —
    belongs to a downtrend."""
    bar = bars[-1]
    body, upper, lower, rng = _primitives(bar)
    quality = _ramp(lower / max(body, _EPS), low=2.0, high=3.0) * _ramp(
        upper / max(rng, _EPS), low=0.35, high=0.05
    )
    return quality * _magnitude(rng, atr) * _reversal_context(trend_regime, "downtrend")


def _shooting_star(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Mirror of the hammer: long upper wick, minimal lower wick. Bearish
    reversal — belongs to an uptrend."""
    bar = bars[-1]
    body, upper, lower, rng = _primitives(bar)
    quality = _ramp(upper / max(body, _EPS), low=2.0, high=3.0) * _ramp(
        lower / max(rng, _EPS), low=0.35, high=0.05
    )
    return quality * _magnitude(rng, atr) * _reversal_context(trend_regime, "uptrend")


def _doji(bars: list[DailyBar], atr: float) -> float:
    """Body negligible relative to the bar's own range. Indecision —
    direction-neutral by definition."""
    bar = bars[-1]
    body, _upper, _lower, rng = _primitives(bar)
    quality = _ramp(body / max(rng, _EPS), low=0.15, high=0.02)
    return quality * _magnitude(rng, atr)


def _marubozu_bull(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """A full-bodied up bar: body dominates the range, both wicks
    negligible. Continuation — aligned with an uptrend."""
    bar = bars[-1]
    if not _is_bullish(bar):
        return 0.0
    quality = _body_over_range_ramp(bar, low=0.7, high=0.95)
    _, _, _, rng = _primitives(bar)
    return quality * _magnitude(rng, atr) * _continuation_context(trend_regime, "uptrend")


def _marubozu_bear(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    bar = bars[-1]
    if not _is_bearish(bar):
        return 0.0
    quality = _body_over_range_ramp(bar, low=0.7, high=0.95)
    _, _, _, rng = _primitives(bar)
    return quality * _magnitude(rng, atr) * _continuation_context(trend_regime, "downtrend")


# ─────────────────────────────────────────────────────────────────────
# Two-bar patterns
# ─────────────────────────────────────────────────────────────────────


def _bullish_engulfing(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Current (bullish) body fully covers the prior (bearish) body.
    Bullish reversal — belongs to a downtrend."""
    prior, cur = bars[-2], bars[-1]
    if not (_is_bearish(prior) and _is_bullish(cur)):
        return 0.0
    quality = _engulf_quality(prior, cur, atr)
    _, _, _, rng = _primitives(cur)
    return quality * _magnitude(rng, atr) * _reversal_context(trend_regime, "downtrend")


def _bearish_engulfing(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    prior, cur = bars[-2], bars[-1]
    if not (_is_bullish(prior) and _is_bearish(cur)):
        return 0.0
    quality = _engulf_quality(prior, cur, atr)
    _, _, _, rng = _primitives(cur)
    return quality * _magnitude(rng, atr) * _reversal_context(trend_regime, "uptrend")


def _bullish_harami(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """A small body nested inside the prior (bearish) bar's larger body.
    Bullish reversal — belongs to a downtrend. Magnitude reads the PRIOR
    bar's range: that big bar is the move that matters; the current bar is
    deliberately small, so testing ITS range would penalise the very thing
    that makes this a harami."""
    prior, cur = bars[-2], bars[-1]
    if not _is_bearish(prior):
        return 0.0
    quality = _harami_quality(prior, cur, atr)
    _, _, _, prior_rng = _primitives(prior)
    return quality * _magnitude(prior_rng, atr) * _reversal_context(trend_regime, "downtrend")


def _bearish_harami(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    prior, cur = bars[-2], bars[-1]
    if not _is_bullish(prior):
        return 0.0
    quality = _harami_quality(prior, cur, atr)
    _, _, _, prior_rng = _primitives(prior)
    return quality * _magnitude(prior_rng, atr) * _reversal_context(trend_regime, "uptrend")


def _piercing_line(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Gaps down from a bearish bar, then closes back up through the
    midpoint of its body without fully engulfing it. Bullish reversal —
    belongs to a downtrend.

    The classical definition of a piercing line is explicitly "closes back
    up past the midpoint, but NOT above the prior open" — that upper bound
    is what distinguishes it from a bullish engulfing. ``_penetration_ramp``
    fades the score back down once ``cur`` closes at or beyond the prior
    open, so a fixture that cleanly engulfs doesn't also read as a strong
    piercing line."""
    prior, cur = bars[-2], bars[-1]
    if not (_is_bearish(prior) and _is_bullish(cur)):
        return 0.0
    prior_body = prior.open - prior.close
    penetration = (cur.close - prior.close) / max(prior_body, _EPS)
    penetration_quality = _penetration_ramp(penetration)
    gap_quality = _ramp((prior.close - cur.open) / atr, low=-0.15, high=0.1)
    quality = penetration_quality * gap_quality
    _, _, _, rng = _primitives(cur)
    return quality * _magnitude(rng, atr) * _reversal_context(trend_regime, "downtrend")


def _dark_cloud_cover(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Mirror of the piercing line: gaps up from a bullish bar, closes back
    down through its midpoint. Bearish reversal — belongs to an uptrend."""
    prior, cur = bars[-2], bars[-1]
    if not (_is_bullish(prior) and _is_bearish(cur)):
        return 0.0
    prior_body = prior.close - prior.open
    penetration = (prior.close - cur.close) / max(prior_body, _EPS)
    penetration_quality = _penetration_ramp(penetration)
    gap_quality = _ramp((cur.open - prior.close) / atr, low=-0.15, high=0.1)
    quality = penetration_quality * gap_quality
    _, _, _, rng = _primitives(cur)
    return quality * _magnitude(rng, atr) * _reversal_context(trend_regime, "uptrend")


# ─────────────────────────────────────────────────────────────────────
# Three-bar patterns
# ─────────────────────────────────────────────────────────────────────


def _morning_star(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Big bearish bar, a small indecisive middle bar, then a big bullish
    bar closing back above the first bar's midpoint. Bullish reversal —
    belongs to a downtrend."""
    bar1, bar2, bar3 = bars[-3], bars[-2], bars[-1]
    if not (_is_bearish(bar1) and _is_bullish(bar3)):
        return 0.0
    bar1_strength = _body_over_range_ramp(bar1, low=0.5, high=0.85)
    body2, _upper2, _lower2, _rng2 = _primitives(bar2)
    bar2_small = _ramp(body2 / atr, low=0.6, high=0.15)
    penetration = (bar3.close - bar1.close) / max(bar1.open - bar1.close, _EPS)
    bar3_penetration = _ramp(penetration, low=0.5, high=1.0)

    quality = bar1_strength * bar2_small * bar3_penetration
    _, _, _, rng3 = _primitives(bar3)
    return quality * _magnitude(rng3, atr) * _reversal_context(trend_regime, "downtrend")


def _evening_star(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Mirror of the morning star. Bearish reversal — belongs to an
    uptrend."""
    bar1, bar2, bar3 = bars[-3], bars[-2], bars[-1]
    if not (_is_bullish(bar1) and _is_bearish(bar3)):
        return 0.0
    bar1_strength = _body_over_range_ramp(bar1, low=0.5, high=0.85)
    body2, _upper2, _lower2, _rng2 = _primitives(bar2)
    bar2_small = _ramp(body2 / atr, low=0.6, high=0.15)
    penetration = (bar1.close - bar3.close) / max(bar1.close - bar1.open, _EPS)
    bar3_penetration = _ramp(penetration, low=0.5, high=1.0)

    quality = bar1_strength * bar2_small * bar3_penetration
    _, _, _, rng3 = _primitives(bar3)
    return quality * _magnitude(rng3, atr) * _reversal_context(trend_regime, "uptrend")


def _three_white_soldiers(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Three consecutive strong bullish bars, each closing higher than the
    last. Continuation — aligned with an uptrend."""
    b1, b2, b3 = bars[-3], bars[-2], bars[-1]
    if not (_is_bullish(b1) and _is_bullish(b2) and _is_bullish(b3)):
        return 0.0
    progress = min(b2.close - b1.close, b3.close - b2.close)
    progress_quality = _ramp(progress / atr, low=-0.05, high=0.3)
    body_quality = min(
        _body_over_range_ramp(b1, low=0.55, high=0.85),
        _body_over_range_ramp(b2, low=0.55, high=0.85),
        _body_over_range_ramp(b3, low=0.55, high=0.85),
    )
    quality = progress_quality * body_quality
    rngs = [b.high - b.low for b in (b1, b2, b3)]
    return quality * _magnitude(min(rngs), atr) * _continuation_context(trend_regime, "uptrend")


def _three_black_crows(bars: list[DailyBar], atr: float, trend_regime: str) -> float:
    """Mirror of three white soldiers. Continuation — aligned with a
    downtrend."""
    b1, b2, b3 = bars[-3], bars[-2], bars[-1]
    if not (_is_bearish(b1) and _is_bearish(b2) and _is_bearish(b3)):
        return 0.0
    progress = min(b1.close - b2.close, b2.close - b3.close)
    progress_quality = _ramp(progress / atr, low=-0.05, high=0.3)
    body_quality = min(
        _body_over_range_ramp(b1, low=0.55, high=0.85),
        _body_over_range_ramp(b2, low=0.55, high=0.85),
        _body_over_range_ramp(b3, low=0.55, high=0.85),
    )
    quality = progress_quality * body_quality
    rngs = [b.high - b.low for b in (b1, b2, b3)]
    return quality * _magnitude(min(rngs), atr) * _continuation_context(trend_regime, "downtrend")


# ─────────────────────────────────────────────────────────────────────
# Range patterns — direction-neutral
# ─────────────────────────────────────────────────────────────────────


def _inside_bar(bars: list[DailyBar], atr: float) -> float:
    """Current bar's range sits entirely inside the prior bar's range — a
    coil. Direction-neutral."""
    prior, cur = bars[-2], bars[-1]
    top_margin = prior.high - cur.high
    bottom_margin = cur.low - prior.low
    quality = _ramp(min(top_margin, bottom_margin) / atr, low=-0.05, high=0.2)
    rng = cur.high - cur.low
    return quality * _coil_magnitude(rng, atr)


def _outside_bar(bars: list[DailyBar], atr: float) -> float:
    """Current bar's range fully engulfs the prior bar's range — an
    expansion. Direction-neutral."""
    prior, cur = bars[-2], bars[-1]
    top_margin = cur.high - prior.high
    bottom_margin = prior.low - cur.low
    quality = _ramp(min(top_margin, bottom_margin) / atr, low=-0.05, high=0.2)
    rng = cur.high - cur.low
    return quality * _magnitude(rng, atr)


def _nr7(bars: list[DailyBar], atr: float) -> float:
    """This bar's range is the narrowest of the trailing 7 — a coil.
    Direction-neutral."""
    window = bars[-7:]
    ranges = [b.high - b.low for b in window]
    cur_rng = ranges[-1]
    other_min = min(ranges[:-1])
    quality = _ramp((other_min - cur_rng) / atr, low=-0.1, high=0.25)
    return quality * _coil_magnitude(cur_rng, atr)
