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
        # Real feature passes carry a "quant" block (engine.features.quant
        # .compute_quant()) alongside "technicals" — price_zscore_20,
        # atr_zscore, donchian_pct, sharpe, the trailing returns,
        # realized_vol_pct, corr_benchmark. This mock predates that block
        # and never grew one, which made every quant-driven FitComponent
        # (zscore_stretch, risk_adjusted, donchian_edge, vol_regime_calm, …)
        # sit at NEUTRAL for every offline/CI pass — a real gap, just a
        # historically harmless one until ``best_strategy``'s evidence gate
        # (docs/PLAN_AGGRESSIVE_PROFILE.md §4) started reading "no quant
        # block at all" as indistinguishable from a genuine data outage.
        # Filled in here so the offline path exercises the same shape a
        # real pass does, deterministic per-symbol like everything else in
        # this module.
        "quant": {
            "price_zscore_20": round(-1.5 + seed * 3.0, 3),
            "atr_zscore": round(-1.0 + seed * 2.0, 3),
            "donchian_pct": round(seed * 100.0, 1),
            "sharpe": round(-0.5 + seed * 2.0, 3),
            "ret_21d_pct": round(-5.0 + seed * 14.0, 2),
            "ret_63d_pct": round(-8.0 + seed * 24.0, 2),
            "ret_252d_pct": round(-15.0 + seed * 55.0, 2),
            "realized_vol_pct": round(15.0 + seed * 35.0, 1),
            "corr_benchmark": round(0.2 + seed * 0.6, 3),
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
        # Borrow eligibility. Synthetic, and labelled as such by
        # ``feature_source`` — but PRESENT, because its absence is not
        # neutral: ``shortable_check`` vetoes on unknown borrow, so a
        # missing block would make every short un-testable offline rather
        # than merely un-verified.
        "asset": {
            "tradable": True,
            "fractionable": True,
            "shortable": True,
            "easy_to_borrow": True,
            "name": f"{symbol} (synthetic)",
        },
        "feature_source": "synthetic",
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
