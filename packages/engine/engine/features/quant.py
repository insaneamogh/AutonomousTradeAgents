"""Quantitative measures over daily OHLCV bars — pure, deterministic math.

``compute_technicals`` answers "where is price relative to its moving
averages". This module answers the questions a desk actually sizes on:
how volatile is this name *right now* relative to its own history, is the
recent return stream worth the risk it took, how much of the move is
market beta, and how stretched is price in units of its own noise.

Everything here is a closed-form statistic over the bars we already fetch.
No network, no wall clock, no randomness, no LLM: same bars in, same
numbers out.

Design notes that matter for correctness:

  - **Degrade to None, never guess.** Every measure has its own minimum
    window. When the window isn't met, or the denominator is zero (a flat
    price series has no volatility, so it has no Sharpe), the field is
    ``None``. A silently-wrong Sharpe is worse than an absent one.
  - **Annualization is 252 trading days** for both return and vol, so
    Sharpe/Sortino are the conventional annualized figures.
  - **Three volatility estimators, on purpose.** Close-to-close throws
    away the intraday range; Parkinson uses H/L and is ~5x more efficient
    for the same sample; Garman-Klass adds O/C and handles the drift term.
    Divergence between them is itself a signal (gap risk vs range churn),
    and we already fetch OHLC so the extra two are free.
  - **Excess kurtosis** (i.e. minus 3), so 0.0 means "Gaussian tails".
  - **Beta/correlation align on trading date**, not on list position —
    two symbols can have different bar counts (halts, listings).

Pure stdlib math throughout. numpy would buy nothing at these sizes and
would cost a typed boundary.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from itertools import pairwise

from engine.features.technicals import DailyBar

# Trading days per year — the annualization factor for vol and returns.
TRADING_DAYS = 252

# Default measurement window: ~3 months of trading. Long enough for a
# stable second moment, short enough to describe the *current* regime.
DEFAULT_LOOKBACK = 63

# Absolute floor. Below this nothing here is meaningful.
MIN_QUANT_BARS = 30

# Window for the ATR z-score's reference distribution (ATR% vs its own
# past year). Falls back to whatever history exists above MIN_ZSCORE_OBS.
ATR_ZSCORE_WINDOW = 252
MIN_ZSCORE_OBS = 40


@dataclass(frozen=True)
class QuantFeatures:
    """Deterministic quant block for one symbol. ``None`` = not computable.

    Percentages are in percent units (12.5 means 12.5%), not fractions.
    """

    lookback_days: int

    # ── Volatility ───────────────────────────────────────────────────
    realized_vol_pct: float | None
    """Annualized close-to-close volatility of log returns."""
    parkinson_vol_pct: float | None
    """Annualized Parkinson (high-low range) volatility."""
    garman_klass_vol_pct: float | None
    """Annualized Garman-Klass (OHLC) volatility."""
    vol_of_vol_pct: float | None
    """Stdev of the rolling 20-day realized-vol series, in vol points."""
    atr_pct: float | None
    """ATR-14 as a percent of last close — vol in units a sizer can use."""
    atr_zscore: float | None
    """Current ATR% standardized against its own trailing distribution."""

    # ── Risk-adjusted ────────────────────────────────────────────────
    sharpe: float | None
    """Annualized Sharpe over the lookback, risk-free = 0."""
    sortino: float | None
    """Annualized Sortino (downside deviation only), risk-free = 0."""
    max_drawdown_pct: float | None
    """Worst peak-to-trough close drawdown over the lookback, negative."""
    calmar: float | None
    """Annualized return ÷ |max drawdown|."""

    # ── Relative ─────────────────────────────────────────────────────
    beta_benchmark: float | None
    """OLS beta of daily returns vs the benchmark (SPY) over the lookback."""
    corr_benchmark: float | None
    """Pearson correlation of daily returns vs the benchmark."""
    excess_return_pct: float | None
    """Lookback return minus the benchmark's, in percentage points."""

    # ── Distribution / position ──────────────────────────────────────
    return_skew: float | None
    """Sample skewness of daily log returns. Negative = crash-prone tail."""
    return_kurtosis: float | None
    """Excess kurtosis. >0 = fatter tails than Gaussian."""
    price_zscore_20: float | None
    """(close − SMA20) ÷ stdev(close, 20). The *standardized* mean-reversion
    measure — unlike ``mean_reversion_risk`` this is in units of the name's
    own noise, so 2.0 means the same thing on SPY as on TSLA."""
    donchian_pct: float | None
    """Where close sits in the 20-day high/low channel, 0 (low) → 100 (high)."""

    # ── Returns ──────────────────────────────────────────────────────
    ret_21d_pct: float | None
    ret_63d_pct: float | None
    ret_252d_pct: float | None

    def as_dict(self) -> dict[str, float | int | None]:
        """Prompt/JSON-friendly view. Same keys as the dataclass fields."""
        return dict(asdict(self))


def compute_quant(
    bars: list[DailyBar],
    *,
    benchmark_bars: list[DailyBar] | None = None,
    lookback: int = DEFAULT_LOOKBACK,
) -> QuantFeatures:
    """Quant block for ``bars`` (oldest → newest), optionally vs a benchmark.

    Never raises on thin or degenerate data: every field independently
    degrades to ``None``. Callers get a full-shaped block regardless, so
    prompts and the scanner can render "n/a" rather than branch.
    """
    n = len(bars)
    if n < 2:
        return _empty(lookback)

    closes = [b.close for b in bars]
    log_rets = _log_returns(closes)
    window = min(lookback, len(log_rets))
    rets = log_rets[-window:] if window > 0 else []

    realized = _annualized_vol(rets)
    park = _parkinson_vol(bars[-window:]) if window > 0 else None
    gk = _garman_klass_vol(bars[-window:]) if window > 0 else None

    atr_series = _atr_pct_series(bars, period=14)
    atr_pct = atr_series[-1] if atr_series else None

    mdd = _max_drawdown_pct(closes[-(window + 1):]) if window > 0 else None
    ann_ret = _annualized_return_pct(rets)

    bench = _aligned_benchmark_returns(bars, benchmark_bars, window) if benchmark_bars else None

    return QuantFeatures(
        lookback_days=window,
        realized_vol_pct=realized,
        parkinson_vol_pct=park,
        garman_klass_vol_pct=gk,
        vol_of_vol_pct=_vol_of_vol(log_rets, window=20, lookback=window),
        atr_pct=atr_pct,
        atr_zscore=_zscore_last(atr_series, window=ATR_ZSCORE_WINDOW),
        sharpe=_sharpe(rets),
        sortino=_sortino(rets),
        max_drawdown_pct=mdd,
        calmar=_calmar(ann_ret, mdd),
        beta_benchmark=_beta(bench[0], bench[1]) if bench else None,
        corr_benchmark=_correlation(bench[0], bench[1]) if bench else None,
        excess_return_pct=_excess_return_pct(bench) if bench else None,
        return_skew=_skew(rets),
        return_kurtosis=_excess_kurtosis(rets),
        price_zscore_20=_price_zscore(closes, 20),
        donchian_pct=_donchian_pct(bars, 20),
        ret_21d_pct=_trailing_return_pct(closes, 21),
        ret_63d_pct=_trailing_return_pct(closes, 63),
        ret_252d_pct=_trailing_return_pct(closes, 252),
    )


def relative_strength_ranks(returns_by_symbol: dict[str, float | None]) -> dict[str, float]:
    """Cross-sectional percentile rank of each symbol's return, 0-100.

    Ties share the average rank (the standard "percentile of the sample"
    definition), so a flat universe scores everyone at 50. Symbols whose
    return is ``None`` or non-finite are dropped from the output entirely
    rather than being ranked as zero — an unrankable name must not look
    like the worst name.
    """
    usable = {s: r for s, r in returns_by_symbol.items() if r is not None and math.isfinite(r)}
    n = len(usable)
    if n == 0:
        return {}
    if n == 1:
        return {next(iter(usable)): 50.0}

    out: dict[str, float] = {}
    values = list(usable.values())
    for sym, r in usable.items():
        below = sum(1 for v in values if v < r)
        equal = sum(1 for v in values if v == r)
        # Average rank across the tie block, mapped onto 0..100.
        rank = below + (equal - 1) / 2.0
        out[sym] = round(rank / (n - 1) * 100.0, 1)
    return out


# ─────────────────────────────────────────────────────────────────────
# Return / volatility primitives
# ─────────────────────────────────────────────────────────────────────


def _log_returns(closes: list[float]) -> list[float]:
    """Log returns, skipping any step touching a non-positive price."""
    out: list[float] = []
    for prev, cur in pairwise(closes):
        if prev <= 0 or cur <= 0:
            continue
        out.append(math.log(cur / prev))
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: list[float]) -> float | None:
    """Sample standard deviation. None below 2 observations."""
    if len(xs) < 2:
        return None
    mu = _mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var) if var > 0 else 0.0


def _annualized_vol(rets: list[float]) -> float | None:
    sd = _stdev(rets)
    if sd is None:
        return None
    return _finite(round(sd * math.sqrt(TRADING_DAYS) * 100.0, 3))


def _annualized_return_pct(rets: list[float]) -> float | None:
    """Annualized geometric return implied by the mean log return."""
    if not rets:
        return None
    ann_log = _mean(rets) * TRADING_DAYS
    # exp() overflows for absurd inputs (a 10x single-day print in bad data).
    if ann_log > 50:
        return None
    return _finite(round((math.exp(ann_log) - 1.0) * 100.0, 3))


def _parkinson_vol(bars: list[DailyBar]) -> float | None:
    """Parkinson (1980) high-low range estimator, annualized, in percent.

        sigma = sqrt( 1/(4 ln2 N) * sum( ln(H/L)^2 ) )
    """
    terms = [
        math.log(b.high / b.low) ** 2
        for b in bars
        if b.high > 0 and b.low > 0 and b.high >= b.low
    ]
    if not terms:
        return None
    var = sum(terms) / (4.0 * math.log(2.0) * len(terms))
    return _finite(round(math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100.0, 3))


def _garman_klass_vol(bars: list[DailyBar]) -> float | None:
    """Garman-Klass (1980) OHLC estimator, annualized, in percent.

        sigma^2 = 1/N * sum( 0.5*ln(H/L)^2 − (2ln2 − 1)*ln(C/O)^2 )

    The estimator can go negative on a single pathological bar (close far
    outside a pinned range); we clamp the variance at 0 rather than
    returning NaN from sqrt.
    """
    terms: list[float] = []
    for b in bars:
        if min(b.high, b.low, b.open, b.close) <= 0 or b.high < b.low:
            continue
        hl = math.log(b.high / b.low)
        co = math.log(b.close / b.open)
        terms.append(0.5 * hl * hl - (2.0 * math.log(2.0) - 1.0) * co * co)
    if not terms:
        return None
    var = max(0.0, sum(terms) / len(terms))
    return _finite(round(math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100.0, 3))


def _vol_of_vol(log_rets: list[float], *, window: int, lookback: int) -> float | None:
    """Dispersion of the rolling realized-vol series — is vol itself stable?

    Computed over the trailing ``lookback`` rolling windows, in the same
    (annualized, percent) units as ``realized_vol_pct``.
    """
    if len(log_rets) < window + 2:
        return None
    series: list[float] = []
    for end in range(window, len(log_rets) + 1):
        v = _annualized_vol(log_rets[end - window: end])
        if v is not None:
            series.append(v)
    series = series[-lookback:]
    sd = _stdev(series)
    return None if sd is None else _finite(round(sd, 3))


def _atr_pct_series(bars: list[DailyBar], *, period: int) -> list[float]:
    """Wilder ATR expressed as a percent of that bar's close, one per bar.

    Same smoothing as ``technicals._atr_wilder`` so ``atr_pct`` is exactly
    ``atr_14 / close × 100`` — the two blocks must never disagree.
    """
    if len(bars) < period + 1:
        return []
    trs: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        b = bars[i]
        trs.append(max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close)))

    atr = sum(trs[:period]) / period
    out: list[float] = []
    close_at = bars[period].close
    if close_at > 0:
        out.append(atr / close_at * 100.0)
    for offset, tr in enumerate(trs[period:], start=period + 1):
        atr = (atr * (period - 1) + tr) / period
        close_at = bars[offset].close
        if close_at > 0:
            out.append(atr / close_at * 100.0)
    return [round(v, 4) for v in out]


def _zscore_last(series: list[float], *, window: int) -> float | None:
    """Z-score of the final observation against the trailing ``window``."""
    if len(series) < MIN_ZSCORE_OBS:
        return None
    ref = series[-window:]
    sd = _stdev(ref)
    if sd is None or sd == 0.0:
        return None
    return _finite(round((series[-1] - _mean(ref)) / sd, 3))


# ─────────────────────────────────────────────────────────────────────
# Risk-adjusted
# ─────────────────────────────────────────────────────────────────────


def _sharpe(rets: list[float]) -> float | None:
    """Annualized Sharpe, rf = 0. None on a flat series (0 vol → undefined)."""
    sd = _stdev(rets)
    if sd is None or sd == 0.0:
        return None
    return _finite(round(_mean(rets) / sd * math.sqrt(TRADING_DAYS), 3))


def _sortino(rets: list[float]) -> float | None:
    """Annualized Sortino: mean ÷ downside deviation about 0.

    Downside deviation uses the full sample in the denominator (the
    standard target-semideviation definition), so a series with few
    losers is correctly rewarded rather than divided by a tiny count.
    """
    if len(rets) < 2:
        return None
    downside = [min(r, 0.0) for r in rets]
    dd = math.sqrt(sum(d * d for d in downside) / len(rets))
    if dd == 0.0:
        return None  # no losing day in the window — ratio is undefined, not infinite
    return _finite(round(_mean(rets) / dd * math.sqrt(TRADING_DAYS), 3))


def _max_drawdown_pct(closes: list[float]) -> float | None:
    """Worst peak-to-trough decline, as a negative percent. 0.0 if monotone up."""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for c in closes:
        if c > peak:
            peak = c
        if peak > 0:
            dd = (c / peak - 1.0) * 100.0
            worst = min(worst, dd)
    return _finite(round(worst, 3))


def _calmar(ann_ret_pct: float | None, mdd_pct: float | None) -> float | None:
    if ann_ret_pct is None or mdd_pct is None or mdd_pct >= 0.0:
        return None
    return _finite(round(ann_ret_pct / abs(mdd_pct), 3))


# ─────────────────────────────────────────────────────────────────────
# Relative (vs benchmark)
# ─────────────────────────────────────────────────────────────────────


def _aligned_benchmark_returns(
    bars: list[DailyBar],
    benchmark_bars: list[DailyBar] | None,
    window: int,
) -> tuple[list[float], list[float]] | None:
    """Daily log returns for symbol and benchmark over their common dates.

    Aligning on ``day`` rather than on list index is the whole point: a
    halted or newly-listed name has fewer bars, and index-aligning would
    silently regress today's return against a benchmark return from a
    different week.
    """
    if not benchmark_bars:
        return None
    bench_close: dict[date, float] = {b.day: b.close for b in benchmark_bars}
    common = [b for b in bars if b.day in bench_close]
    if len(common) < 3:
        return None
    sym_rets = _log_returns([b.close for b in common])
    ben_rets = _log_returns([bench_close[b.day] for b in common])
    if len(sym_rets) != len(ben_rets) or len(sym_rets) < 2:
        return None
    take = min(window, len(sym_rets))
    return sym_rets[-take:], ben_rets[-take:]


def _beta(sym: list[float], bench: list[float]) -> float | None:
    """OLS beta = cov(sym, bench) / var(bench). None when the market is flat."""
    if len(sym) < 2:
        return None
    mb = _mean(bench)
    var_b = sum((b - mb) ** 2 for b in bench)
    if var_b <= 0:
        return None
    ms = _mean(sym)
    cov = sum((s - ms) * (b - mb) for s, b in zip(sym, bench, strict=True))
    return _finite(round(cov / var_b, 3))


def _correlation(sym: list[float], bench: list[float]) -> float | None:
    if len(sym) < 2:
        return None
    ms, mb = _mean(sym), _mean(bench)
    num = sum((s - ms) * (b - mb) for s, b in zip(sym, bench, strict=True))
    den_s = math.sqrt(sum((s - ms) ** 2 for s in sym))
    den_b = math.sqrt(sum((b - mb) ** 2 for b in bench))
    if den_s == 0.0 or den_b == 0.0:
        return None
    return _finite(round(num / (den_s * den_b), 3))


def _excess_return_pct(bench: tuple[list[float], list[float]]) -> float | None:
    """Cumulative symbol return minus benchmark return over the window, in pp."""
    sym, ben = bench
    if not sym:
        return None
    s = sum(sym)
    b = sum(ben)
    if abs(s) > 50 or abs(b) > 50:
        return None
    return _finite(round(((math.exp(s) - 1.0) - (math.exp(b) - 1.0)) * 100.0, 3))


# ─────────────────────────────────────────────────────────────────────
# Distribution / position
# ─────────────────────────────────────────────────────────────────────


def _skew(rets: list[float]) -> float | None:
    """Sample skewness g1 = m3 / m2^1.5. None below 3 obs or on zero variance."""
    n = len(rets)
    if n < 3:
        return None
    mu = _mean(rets)
    m2 = sum((r - mu) ** 2 for r in rets) / n
    if m2 <= 0:
        return None
    m3 = sum((r - mu) ** 3 for r in rets) / n
    return _finite(round(m3 / (m2 ** 1.5), 3))


def _excess_kurtosis(rets: list[float]) -> float | None:
    """Excess kurtosis g2 = m4 / m2^2 − 3. 0.0 means Gaussian tails."""
    n = len(rets)
    if n < 4:
        return None
    mu = _mean(rets)
    m2 = sum((r - mu) ** 2 for r in rets) / n
    if m2 <= 0:
        return None
    m4 = sum((r - mu) ** 4 for r in rets) / n
    return _finite(round(m4 / (m2 * m2) - 3.0, 3))


def _price_zscore(closes: list[float], window: int) -> float | None:
    """(last − SMA) ÷ stdev over ``window`` closes. None on a flat window."""
    if len(closes) < window:
        return None
    ref = closes[-window:]
    sd = _stdev(ref)
    if sd is None or sd == 0.0:
        return None
    return _finite(round((ref[-1] - _mean(ref)) / sd, 3))


def _donchian_pct(bars: list[DailyBar], window: int) -> float | None:
    """Position of the last close inside the N-day high/low channel, 0-100."""
    if len(bars) < window:
        return None
    ref = bars[-window:]
    hi = max(b.high for b in ref)
    lo = min(b.low for b in ref)
    if hi <= lo:
        return None
    pos = (ref[-1].close - lo) / (hi - lo) * 100.0
    return _finite(round(max(0.0, min(100.0, pos)), 2))


def _trailing_return_pct(closes: list[float], window: int) -> float | None:
    if len(closes) < window + 1:
        return None
    base = closes[-(window + 1)]
    if base <= 0:
        return None
    return _finite(round((closes[-1] / base - 1.0) * 100.0, 3))


# ─────────────────────────────────────────────────────────────────────
# Guards
# ─────────────────────────────────────────────────────────────────────


def _finite(x: float) -> float | None:
    """None for NaN/inf. Every public number goes through this."""
    return x if math.isfinite(x) else None


def _empty(lookback: int) -> QuantFeatures:
    """All-None block — returned when there is nothing to compute from."""
    return QuantFeatures(
        lookback_days=0,
        realized_vol_pct=None,
        parkinson_vol_pct=None,
        garman_klass_vol_pct=None,
        vol_of_vol_pct=None,
        atr_pct=None,
        atr_zscore=None,
        sharpe=None,
        sortino=None,
        max_drawdown_pct=None,
        calmar=None,
        beta_benchmark=None,
        corr_benchmark=None,
        excess_return_pct=None,
        return_skew=None,
        return_kurtosis=None,
        price_zscore_20=None,
        donchian_pct=None,
        ret_21d_pct=None,
        ret_63d_pct=None,
        ret_252d_pct=None,
    )
