"""Position sizer (Phase 1).

Public surface:
    from engine.sizing import (
        atr_position_size,
        AtrSizingConfig,
        SizingInputs,
        SizingDecision,
    )

Default: volatility-targeted (ATR-based, PLAN.md §6.3).
Opt-in: Kelly fraction for advanced users — Phase 2.
Never: percent-of-account fixed (with the deliberate fallback when ATR is
unavailable; method='fallback_pct' so callers can surface it).
"""

from engine.sizing.atr import AtrSizingConfig, atr_position_size
from engine.sizing.types import SizingDecision, SizingInputs

__all__ = [
    "AtrSizingConfig",
    "SizingDecision",
    "SizingInputs",
    "atr_position_size",
]
