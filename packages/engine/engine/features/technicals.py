"""Technical indicators over daily OHLCV bars — pure, deterministic math.

Produces the exact ``technicals`` dict shape the agent prompts already cite
(``rsi_14``, ``dma50_pct``, ``volume_ratio_20d``, …), so the council swaps
from synthetic to real features without a single prompt change.

Indicator conventions:
  - ATR-14 / RSI-14 use Wilder smoothing (the classical definitions).
  - ``dmaN_pct`` = percent distance of the last close from the N-day SMA.
  - ``vwap_position`` is a daily-bar proxy: last close vs the 20-day
    rolling VWAP (typical price × volume). True intraday VWAP arrives with
    intraday bars in v1.5.
  - ``mean_reversion_risk`` / ``trend_position_score`` are documented
    0-100 heuristics (see functions) — deterministic composites, not
    indicators with textbook definitions.

No network, no wall clock, no randomness: same bars in, same dict out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


class InsufficientBarsError(ValueError):
    """Raised when there isn't enough history to compute the core set."""


@dataclass(frozen=True)
class DailyBar:
    """One daily OHLCV bar. ``day`` is the trading date (exchange-local)."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


# Core indicators need RSI/ATR (15 bars) + SMA50 + a stable volume base.
MIN_BARS = 60


def compute_technicals(bars: list[DailyBar]) -> dict:
    """The council's ``technicals`` feature block from daily bars.

    Bars must be oldest → newest. Raises ``InsufficientBarsError`` below
    ``MIN_BARS``; ``dma200_pct`` is None when fewer than 200 bars exist
    (prompts already render missing values as 'n/a').
    """
    if len(bars) < MIN_BARS:
        raise InsufficientBarsError(
            f"need >= {MIN_BARS} daily bars, got {len(bars)}"
        )

    closes = [b.close for b in bars]
    last_close = closes[-1]

    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200) if len(closes) >= 200 else None

    rsi = _rsi_wilder(closes, 14)
    atr = _atr_wilder(bars, 14)

    return {
        "trend_regime": _trend_regime(last_close, sma50, sma200),
        "dma20_pct": _pct_from(last_close, sma20),
        "dma50_pct": _pct_from(last_close, sma50),
        "dma200_pct": _pct_from(last_close, sma200) if sma200 is not None else None,
        "rsi_14": round(rsi, 1),
        "atr_14": round(atr, 4),
        "vwap_position": "above" if last_close >= _rolling_vwap(bars, 20) else "below",
        "mean_reversion_risk": _mean_reversion_risk(rsi),
        "trend_position_score": _trend_position_score(last_close, sma20, sma50, sma200),
        "volume_ratio_20d": _volume_ratio(bars, 20),
    }


# ─────────────────────────────────────────────────────────────────────
# Primitives
# ─────────────────────────────────────────────────────────────────────


def _sma(values: list[float], window: int) -> float:
    return sum(values[-window:]) / window


def _pct_from(last: float, base: float) -> float:
    return round((last / base - 1.0) * 100.0, 2)


def _rsi_wilder(closes: list[float], period: int) -> float:
    """Classical Wilder RSI. 100 when there are no losses in the window."""
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr_wilder(bars: list[DailyBar], period: int) -> float:
    trs: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        b = bars[i]
        trs.append(
            max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        )
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _rolling_vwap(bars: list[DailyBar], window: int) -> float:
    recent = bars[-window:]
    weighted = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in recent)
    total_vol = sum(b.volume for b in recent)
    if total_vol <= 0:
        return recent[-1].close
    return weighted / total_vol


def _volume_ratio(bars: list[DailyBar], window: int) -> float:
    recent = bars[-window:]
    avg = sum(b.volume for b in recent) / len(recent)
    if avg <= 0:
        return 1.0
    return round(bars[-1].volume / avg, 2)


def _trend_regime(last: float, sma50: float, sma200: float | None) -> str:
    if sma200 is None:
        # Not enough history for the full stack — fall back to the 50-day.
        return "uptrend" if last > sma50 else "choppy"
    if last > sma50 > sma200:
        return "uptrend"
    if last < sma50 < sma200:
        return "downtrend"
    return "choppy"


def _mean_reversion_risk(rsi: float) -> float:
    """0-100 heuristic: how stretched is RSI from neutral (50)?

    RSI 50 → 0 (no stretch), RSI 80 or 20 → 60, RSI 100 or 0 → 100. The
    analysts read this as 'risk that the next move is a snap-back'.
    """
    return round(min(100.0, abs(rsi - 50.0) * 2.0), 1)


def _trend_position_score(
    last: float, sma20: float, sma50: float, sma200: float | None
) -> float:
    """0-100 composite of trend alignment. 25 points each for: close>SMA20,
    close>SMA50, close>SMA200, SMA50>SMA200. With <200 bars the two
    SMA200 components are awarded at half-credit (neutral, not penalized).
    """
    score = 0.0
    score += 25.0 if last > sma20 else 0.0
    score += 25.0 if last > sma50 else 0.0
    if sma200 is None:
        score += 25.0  # 12.5 + 12.5 neutral credit for the two unknown legs
    else:
        score += 25.0 if last > sma200 else 0.0
        score += 25.0 if sma50 > sma200 else 0.0
    return round(score, 1)
