"""Synthetic features so the agent loop is runnable offline.

Deterministic per symbol — same ticker yields the same features every run,
which keeps the smoke output stable.
"""

from __future__ import annotations

from typing import Any


def _hash_seed(symbol: str) -> float:
    """Stable 0-1 from the symbol — small, deterministic spread."""
    h = sum(ord(c) * (i + 1) for i, c in enumerate(symbol))
    return ((h % 997) / 997.0)


def synthetic_features(symbol: str, horizon: str = "short") -> dict[str, Any]:
    seed = _hash_seed(symbol)
    last_price = round(50.0 + seed * 250.0, 2)
    # ATR scales with price + adds per-symbol vol variation (1.2% – 3.7% of price).
    # Real Phase 1 feature provider replaces this with a true 14-day ATR.
    atr_14 = round(last_price * (0.012 + seed * 0.025), 4)
    return {
        "symbol": symbol,
        "horizon": horizon,
        "universe": "US",
        "last_price": last_price,
        "portfolio_equity": 100_000.0,
        "technicals": {
            "trend_regime": "uptrend" if seed > 0.3 else "choppy",
            "dma20_pct": round(-2.0 + seed * 6.0, 2),
            "dma50_pct": round(-3.0 + seed * 8.0, 2),
            "dma200_pct": round(-10.0 + seed * 28.0, 2),
            "rsi_14": round(40.0 + seed * 30.0, 1),
            "atr_14": atr_14,
            "vwap_position": "above" if seed > 0.4 else "below",
            "mean_reversion_risk": round(20.0 + seed * 30.0, 1),
            "trend_position_score": round(40.0 + seed * 40.0, 1),
            "volume_ratio_20d": round(0.8 + seed * 0.6, 2),
        },
        "fundamentals": {
            "quality_score": round(40.0 + seed * 40.0, 1),
            "business_quality_score": round(45.0 + seed * 35.0, 1),
            "earnings_power_score": round(35.0 + seed * 45.0, 1),
            "valuation_score": round(30.0 + seed * 40.0, 1),
            "growth_trajectory": round(35.0 + seed * 45.0, 1),
            "capital_efficiency": round(40.0 + seed * 40.0, 1),
            "shareholder_returns": round(30.0 + seed * 50.0, 1),
            "piotroski_f_score": int(3 + seed * 6),
        },
        # Phase 1: macro values are constant-per-day for a real ingest from FRED.
        # Phase 0 synthesizes a plausible spread per-symbol so the Macro Analyst
        # has something to chew on. Phase 2 swaps in the real feature-store call.
        "macro": {
            "vix_level": round(14.0 + seed * 10.0, 1),               # 14 – 24
            "ten_year_yield_pct": round(3.4 + seed * 1.3, 2),         # 3.4 – 4.7
            "dxy_index": round(100.0 + seed * 8.0, 1),               # 100 – 108
            "sector_relative_strength": round(-3.0 + seed * 8.0, 2),  # -3% to +5% vs SPY (21d)
        },
    }
