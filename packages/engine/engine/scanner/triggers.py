"""Deterministic trigger rules — pure Python, zero LLM, named identifiers.

This is the cheap half of the two-tier scan. It runs every few minutes over
the whole watchlist and costs one batched market-data request; the expensive
half (a six-node LLM council at ~$0.066/symbol) only runs on what fires here.

Every rule is a pure function ``(SymbolSnapshot, ScannerConfig) -> ScanSignal
| None``. No I/O, no clock, no state — the snapshot carries the observation
instant. That makes the whole trigger layer exhaustively testable with
hand-built snapshots, which matters because this layer decides how the
budget is spent.

**The shape of a cross.** All six moving-average rules compare a SETTLED
level (computed from daily bars that closed yesterday) against a LIVE price
(today's most recent intraday print). A cross is a genuine disagreement
between the two: yesterday's close was below the 20-DMA, right now price is
above it. Comparing live price to a level recomputed from live price would
trigger on nothing at all.

**Why a buffer.** ``cross_buffer_pct`` requires the price to clear the level
by a tenth of a percent. A name pinned to its 50-DMA otherwise re-fires on
every single scan as the last print oscillates around the line — which is
the exact failure mode that turns a cost-saving scanner into a cost
multiplier.

**Strength** is 0..1 and comparable only within a rule. It answers "how far
past the threshold", never "how good is this trade".
"""

from __future__ import annotations

from collections.abc import Callable

from engine.scanner.types import (
    Direction,
    ScannerConfig,
    ScanSignal,
    SymbolSnapshot,
    TriggerRule,
)

TriggerFn = Callable[[SymbolSnapshot, ScannerConfig], "ScanSignal | None"]


def evaluate_triggers(
    snap: SymbolSnapshot, config: ScannerConfig | None = None
) -> list[ScanSignal]:
    """Every rule that fires for ``snap``, in registry order.

    Returns an empty list — never raises — when the snapshot is too thin to
    evaluate. A symbol with no intraday prints yet (halted, or scanned in
    the first minutes of the session before IEX has printed) has nothing to
    cross a level with.
    """
    cfg = config or ScannerConfig()
    if not snap.has_intraday or snap.last_price <= 0 or snap.prior_close <= 0:
        return []
    out: list[ScanSignal] = []
    for fn in TRIGGERS:
        signal = fn(snap, cfg)
        if signal is not None:
            out.append(signal)
    return out


# ─────────────────────────────────────────────────────────────────────
# Moving-average crosses
# ─────────────────────────────────────────────────────────────────────


def _ma_cross(
    snap: SymbolSnapshot,
    cfg: ScannerConfig,
    *,
    level: float | None,
    up_rule: str,
    down_rule: str,
    label: str,
) -> ScanSignal | None:
    """Shared body for all six DMA cross rules.

    Extracted rather than copy-pasted six times because the buffer logic is
    the part that is easy to get subtly wrong, and one copy of it is one
    thing to test.
    """
    if level is None or level <= 0:
        return None
    buffer = level * cfg.cross_buffer_pct / 100.0

    crossed_up = snap.prior_close <= level and snap.last_price > level + buffer
    crossed_down = snap.prior_close >= level and snap.last_price < level - buffer
    if not (crossed_up or crossed_down):
        return None

    distance_pct = abs(snap.last_price / level - 1.0) * 100.0
    rule = up_rule if crossed_up else down_rule
    direction: Direction = "bullish" if crossed_up else "bearish"
    verb = "above" if crossed_up else "below"
    return ScanSignal(
        symbol=snap.symbol,
        trigger_rule=rule,
        strength=_saturate(distance_pct / 2.0),
        observed_at=snap.observed_at,
        direction=direction,
        detail=(
            f"{snap.last_price:.2f} crossed {verb} the {label} at {level:.2f} "
            f"(prior close {snap.prior_close:.2f}, {distance_pct:.2f}% through)"
        ),
        context={
            "last_price": snap.last_price,
            "prior_close": snap.prior_close,
            "level": round(level, 4),
            "distance_pct": round(distance_pct, 3),
        },
    )


def dma20_cross(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """Price crossed the 20-day SMA since yesterday's close."""
    return _ma_cross(
        snap, cfg,
        level=snap.sma20,
        up_rule=TriggerRule.DMA20_CROSS_UP,
        down_rule=TriggerRule.DMA20_CROSS_DOWN,
        label="20-DMA",
    )


def dma50_cross(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """Price crossed the 50-day SMA since yesterday's close."""
    return _ma_cross(
        snap, cfg,
        level=snap.sma50,
        up_rule=TriggerRule.DMA50_CROSS_UP,
        down_rule=TriggerRule.DMA50_CROSS_DOWN,
        label="50-DMA",
    )


def dma200_cross(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """Price crossed the 200-day SMA — the regime line."""
    return _ma_cross(
        snap, cfg,
        level=snap.sma200,
        up_rule=TriggerRule.DMA200_CROSS_UP,
        down_rule=TriggerRule.DMA200_CROSS_DOWN,
        label="200-DMA",
    )


# ─────────────────────────────────────────────────────────────────────
# RSI band transitions
# ─────────────────────────────────────────────────────────────────────


def rsi_band_transition(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """RSI-14 entered or left an extreme band once live price is folded in.

    ``rsi_prior`` is RSI on settled closes; ``rsi_live`` re-runs the same
    Wilder recursion with today's live price standing in for today's close.
    A band transition is the two disagreeing.

    Leaving a band is the actionable event for a mean-reversion book (the
    snap-back has started); entering one is the actionable event for a
    trend book (the move is extending). Both are emitted, tagged by
    direction, and the council decides which reading applies.
    """
    prior, live = snap.rsi_prior, snap.rsi_live
    if prior is None or live is None:
        return None

    lo, hi = cfg.rsi_oversold, cfg.rsi_overbought
    rule: str | None = None
    direction: Direction = "bullish"
    if prior <= lo < live:
        rule, direction = TriggerRule.RSI_EXIT_OVERSOLD, "bullish"
    elif prior >= hi > live:
        rule, direction = TriggerRule.RSI_EXIT_OVERBOUGHT, "bearish"
    elif prior > lo >= live:
        rule, direction = TriggerRule.RSI_ENTER_OVERSOLD, "bearish"
    elif prior < hi <= live:
        rule, direction = TriggerRule.RSI_ENTER_OVERBOUGHT, "bullish"
    if rule is None:
        return None

    return ScanSignal(
        symbol=snap.symbol,
        trigger_rule=rule,
        strength=_saturate(abs(live - prior) / 10.0),
        observed_at=snap.observed_at,
        direction=direction,
        detail=f"RSI-14 moved {prior:.1f} → {live:.1f} across the {lo:.0f}/{hi:.0f} bands",
        context={"rsi_prior": round(prior, 2), "rsi_live": round(live, 2)},
    )


# ─────────────────────────────────────────────────────────────────────
# Volume
# ─────────────────────────────────────────────────────────────────────


def volume_spike(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """Today's cumulative volume is a multiple of the 20-day average.

    Cumulative-so-far vs a FULL-day average is deliberately conservative:
    early in the session the ratio is structurally small, so this cannot
    fire at 09:35 on a normal day. The cost of that conservatism is a late
    trigger on a genuine volume day; the cost of the alternative (an
    intraday-profile-adjusted ratio) is a profile model we have no data to
    calibrate and every incentive to overfit.

    Both sides use IEX volume, so the venue's ~2-3% share of consolidated
    tape cancels out of the ratio.
    """
    avg = snap.avg_volume_20d
    if avg is None or avg <= 0 or snap.session_volume <= 0:
        return None
    ratio = snap.session_volume / avg
    if ratio < cfg.volume_spike_mult:
        return None

    strong = ratio >= cfg.volume_spike_strong_mult
    rule = TriggerRule.VOLUME_SPIKE_3X if strong else TriggerRule.VOLUME_SPIKE_2X
    # Volume alone has no sign. Direction follows the day's price move,
    # which is what makes the signal readable: heavy volume into a decline
    # is not the same event as heavy volume into a rally.
    direction: Direction = "bullish" if snap.last_price >= snap.prior_close else "bearish"
    span = max(cfg.volume_spike_strong_mult - cfg.volume_spike_mult, 1e-9)
    return ScanSignal(
        symbol=snap.symbol,
        trigger_rule=rule,
        strength=_saturate((ratio - cfg.volume_spike_mult) / span),
        observed_at=snap.observed_at,
        direction=direction,
        detail=f"session volume {ratio:.2f}x the 20-day average",
        context={
            "session_volume": snap.session_volume,
            "avg_volume_20d": round(avg, 2),
            "volume_ratio": round(ratio, 3),
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Volatility
# ─────────────────────────────────────────────────────────────────────


def atr_expansion(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """Today's true range has blown past the 14-day ATR.

    True range uses the session high/low AND the prior close, so an
    overnight gap counts toward the range exactly as Wilder defined it —
    a 4% gap that then goes quiet is still a volatility event.
    """
    atr = snap.atr_14
    if atr is None or atr <= 0 or snap.session_high is None or snap.session_low is None:
        return None
    true_range = max(
        snap.session_high - snap.session_low,
        abs(snap.session_high - snap.prior_close),
        abs(snap.session_low - snap.prior_close),
    )
    ratio = true_range / atr
    if ratio < cfg.atr_expansion_mult:
        return None
    direction: Direction = "bullish" if snap.last_price >= snap.prior_close else "bearish"
    return ScanSignal(
        symbol=snap.symbol,
        trigger_rule=TriggerRule.ATR_EXPANSION,
        strength=_saturate((ratio - cfg.atr_expansion_mult) / cfg.atr_expansion_mult),
        observed_at=snap.observed_at,
        direction=direction,
        detail=f"true range {true_range:.2f} is {ratio:.2f}x ATR-14 ({atr:.2f})",
        context={
            "true_range": round(true_range, 4),
            "atr_14": round(atr, 4),
            "atr_ratio": round(ratio, 3),
        },
    )


def gap_from_prior_close(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """The session opened materially away from yesterday's close."""
    if snap.session_open is None or snap.session_open <= 0:
        return None
    gap_pct = (snap.session_open / snap.prior_close - 1.0) * 100.0
    if abs(gap_pct) < cfg.gap_pct:
        return None
    up = gap_pct > 0
    return ScanSignal(
        symbol=snap.symbol,
        trigger_rule=TriggerRule.GAP_UP if up else TriggerRule.GAP_DOWN,
        strength=_saturate((abs(gap_pct) - cfg.gap_pct) / cfg.gap_pct),
        observed_at=snap.observed_at,
        direction="bullish" if up else "bearish",
        detail=(
            f"opened {gap_pct:+.2f}% from the prior close "
            f"({snap.prior_close:.2f} → {snap.session_open:.2f})"
        ),
        context={
            "session_open": snap.session_open,
            "prior_close": snap.prior_close,
            "gap_pct": round(gap_pct, 3),
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Donchian channel
# ─────────────────────────────────────────────────────────────────────


def donchian_break(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """Live price broke the 20-day high or the 10-day low.

    This is the ``breakout`` reference strategy's own entry and exit
    condition (20-up / 10-down), evaluated live rather than at the close.
    The asymmetry is intentional and comes from the strategy: cut faster
    than you enter.
    """
    hi, lo = snap.donchian_high_20, snap.donchian_low_10
    if hi is not None and hi > 0 and snap.last_price > hi:
        excess = (snap.last_price / hi - 1.0) * 100.0
        return ScanSignal(
            symbol=snap.symbol,
            trigger_rule=TriggerRule.DONCHIAN_BREAKOUT_UP,
            strength=_saturate(excess / 2.0),
            observed_at=snap.observed_at,
            direction="bullish",
            detail=f"{snap.last_price:.2f} broke the 20-day high {hi:.2f} (+{excess:.2f}%)",
            context={"last_price": snap.last_price, "donchian_high_20": round(hi, 4)},
        )
    if lo is not None and lo > 0 and snap.last_price < lo:
        excess = (1.0 - snap.last_price / lo) * 100.0
        return ScanSignal(
            symbol=snap.symbol,
            trigger_rule=TriggerRule.DONCHIAN_BREAKDOWN,
            strength=_saturate(excess / 2.0),
            observed_at=snap.observed_at,
            direction="bearish",
            detail=f"{snap.last_price:.2f} broke the 10-day low {lo:.2f} (−{excess:.2f}%)",
            context={"last_price": snap.last_price, "donchian_low_10": round(lo, 4)},
        )
    return None


def donchian_approach(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """Price is in the top/bottom decile of its 20-day channel, not through it.

    Mutually exclusive with ``donchian_break`` by construction — once price
    is through the edge the break rule owns the event. Waking the council
    *before* the break is the point: a proposal that arrives after the
    breakout has already run is a proposal that chases.

    Position in the channel, not distance in percent. The percent version
    fires permanently on any name whose 20-day range is narrower than the
    tolerance, which selects for the quietest names in the universe —
    exactly backwards.
    """
    hi, lo20 = snap.donchian_high_20, snap.donchian_low_20
    if hi is None or lo20 is None or hi <= lo20:
        return None
    if snap.last_price > hi or (snap.donchian_low_10 is not None
                                and snap.last_price < snap.donchian_low_10):
        return None  # already broken — the break rule owns this

    position = (snap.last_price - lo20) / (hi - lo20) * 100.0
    band = cfg.donchian_approach_band_pct

    if position >= 100.0 - band:
        return ScanSignal(
            symbol=snap.symbol,
            trigger_rule=TriggerRule.DONCHIAN_UPPER_APPROACH,
            strength=_saturate((position - (100.0 - band)) / band),
            observed_at=snap.observed_at,
            direction="bullish",
            detail=(
                f"{snap.last_price:.2f} sits at {position:.0f}% of the 20-day "
                f"channel ({lo20:.2f}-{hi:.2f}) — approaching the high"
            ),
            context={
                "last_price": snap.last_price,
                "donchian_high_20": round(hi, 4),
                "donchian_low_20": round(lo20, 4),
                "channel_position_pct": round(position, 2),
            },
        )
    if position <= band:
        return ScanSignal(
            symbol=snap.symbol,
            trigger_rule=TriggerRule.DONCHIAN_LOWER_APPROACH,
            strength=_saturate((band - position) / band),
            observed_at=snap.observed_at,
            direction="bearish",
            detail=(
                f"{snap.last_price:.2f} sits at {position:.0f}% of the 20-day "
                f"channel ({lo20:.2f}-{hi:.2f}) — approaching the low"
            ),
            context={
                "last_price": snap.last_price,
                "donchian_high_20": round(hi, 4),
                "donchian_low_20": round(lo20, 4),
                "channel_position_pct": round(position, 2),
            },
        )
    return None


# ─────────────────────────────────────────────────────────────────────
# Standardized stretch
# ─────────────────────────────────────────────────────────────────────


def zscore_stretch(snap: SymbolSnapshot, cfg: ScannerConfig) -> ScanSignal | None:
    """Live price is >= N standard deviations from its own 20-day mean.

    This is the scanner's mean-reversion trigger and it is standardized on
    purpose: a 3% move is noise on TSLA (58% annualized vol) and a genuine
    two-sigma event on COST (22%). A percent-distance rule would fire
    constantly on the volatile names and never on the calm ones — which is
    precisely backwards, since the calm name's 2-sigma move is the one
    carrying information.
    """
    mean, sd = snap.close_mean_20, snap.close_std_20
    if mean is None or sd is None or sd <= 0:
        return None
    z = (snap.last_price - mean) / sd
    if abs(z) < cfg.zscore_threshold:
        return None
    up = z > 0
    return ScanSignal(
        symbol=snap.symbol,
        trigger_rule=TriggerRule.ZSCORE_STRETCH_UP if up else TriggerRule.ZSCORE_STRETCH_DOWN,
        strength=_saturate((abs(z) - cfg.zscore_threshold) / cfg.zscore_threshold),
        observed_at=snap.observed_at,
        direction="bullish" if up else "bearish",
        detail=f"price is {z:+.2f}σ from its 20-day mean ({mean:.2f}, σ={sd:.2f})",
        context={
            "price_zscore": round(z, 3),
            "close_mean_20": round(mean, 4),
            "close_std_20": round(sd, 4),
        },
    )


#: Evaluation order. Deterministic so two scans over identical snapshots
#: emit identical signal lists — the property the cooldown relies on.
TRIGGERS: tuple[TriggerFn, ...] = (
    dma20_cross,
    dma50_cross,
    dma200_cross,
    rsi_band_transition,
    volume_spike,
    atr_expansion,
    gap_from_prior_close,
    donchian_break,
    donchian_approach,
    zscore_stretch,
)


def _saturate(x: float) -> float:
    """Clamp a raw over-threshold ratio into the 0.05..1.0 strength band.

    The 0.05 floor keeps a barely-past-threshold trigger from reporting
    strength 0.0, which reads as "didn't fire" everywhere it is rendered.
    """
    if x != x:  # NaN
        return 0.05
    return max(0.05, min(1.0, x))
