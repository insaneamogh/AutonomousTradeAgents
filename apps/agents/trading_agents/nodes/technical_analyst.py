"""Technical analyst node — reads pre-computed technical features. Haiku-tier.

Cheapest tier on purpose: reading already-computed indicators into a 0-100
score is pattern-matching, not reasoning. The judgement calls live in Macro
(regime) and the Drafter (thesis).

Four feature blocks are rendered, all computed deterministically upstream
(``engine.features`` / ``engine.scanner``) — the analyst never fetches or
derives anything:

  - ``technicals``    — moving-average distances, RSI, ATR, volume ratio.
  - ``quant``         — vol regime, risk-adjusted return, beta, tail shape,
                        standardized price z-score. Optional: the block is
                        absent under the synthetic MOCK provider.
  - ``patterns``      — candlestick pattern scores (hammer, engulfing,
                        marubozu, …), already ATR-normalised and
                        trend-context-gated. Optional: absent under the
                        synthetic MOCK provider, same as ``quant``.
  - ``scan_triggers`` — why the scanner woke the council for this symbol
                        right now. Absent on a scheduled full sweep.
"""

from __future__ import annotations

from typing import Any

from trading_agents.llm import LLM, Model
from trading_agents.nodes._specialist import render_features, run_specialist
from trading_agents.prompts import TECHNICAL_ANALYST
from trading_agents.state import CouncilState

FEATURES = (
    "trend_regime",
    "dma20_pct",
    "dma50_pct",
    "dma200_pct",
    "rsi_14",
    "vwap_position",
    "mean_reversion_risk",
    "trend_position_score",
    "volume_ratio_20d",
)

#: Quant fields worth the prompt tokens. The block computes more than this
#: (Parkinson/Garman-Klass vol, calmar, vol-of-vol); those are for the
#: scanner and the sizer, which read numbers rather than prose. What is
#: listed here is what changes a 0-100 technical score.
QUANT_FEATURES = (
    "realized_vol_pct",
    "atr_pct",
    "atr_zscore",
    "sharpe",
    "sortino",
    "max_drawdown_pct",
    "beta_benchmark",
    "corr_benchmark",
    "return_skew",
    "return_kurtosis",
    "price_zscore_20",
    "donchian_pct",
    "ret_21d_pct",
    "ret_63d_pct",
    "relative_strength_rank",
)

#: Candlestick pattern fields worth the prompt tokens. ``names`` is
#: deliberately excluded — it is a tuple, and ``render_features`` expects
#: scalars (missing keys render as ``n/a``, not a tuple's ``repr``);
#: ``top_pattern`` already carries the headline.
PATTERN_FEATURES = (
    "top_pattern",
    "top_pattern_score",
    "reversal_bull",
    "reversal_bear",
    "continuation_bull",
    "continuation_bear",
    "indecision",
    "compression",
    "expansion",
)


async def technical_analyst_node(state: CouncilState, llm: LLM) -> CouncilState:
    """Score the symbol's technical setup 0-100 from the context blocks."""
    ctx: dict[str, Any] = state.get("context", {})
    body = render_features(ctx.get("technicals", {}), FEATURES, label_width=25)

    quant = ctx.get("quant")
    if quant:
        body += "\nQuant measures:\n" + render_features(
            quant, QUANT_FEATURES, label_width=25
        )

    patterns = ctx.get("patterns")
    if patterns:
        body += "\nCandlestick patterns:\n" + render_features(
            patterns, PATTERN_FEATURES, label_width=25
        )

    body += _render_triggers(ctx.get("scan_triggers"))

    return await run_specialist(
        state,
        llm,
        name="technical",
        system=TECHNICAL_ANALYST,
        model=Model.HAIKU,
        header=(
            f"Ticker: {state['symbol']}\n"
            f"Horizon: {state.get('horizon', 'short')}\n\n"
            "Technical features:\n"
        ),
        body=body,
    )


def _render_triggers(triggers: Any) -> str:
    """Render the scanner's trigger list, or nothing when there isn't one.

    Each line is ``rule (strength) — detail``. The rule identifier is the
    same named string the scanner logged and the audit row carries, so a
    thesis citing ``dma20_cross_up`` is traceable to the exact scan that
    fired it.
    """
    if not triggers:
        return ""
    lines = ["\nScan triggers (deterministic — why this symbol woke the council):"]
    for t in triggers:
        rule = t.get("rule", "?") if isinstance(t, dict) else str(t)
        strength = t.get("strength") if isinstance(t, dict) else None
        detail = t.get("detail", "") if isinstance(t, dict) else ""
        strength_txt = f" (strength {strength:.2f})" if isinstance(strength, int | float) else ""
        lines.append(f"  - {rule}{strength_txt}{f' — {detail}' if detail else ''}")
    return "\n".join(lines) + "\n"
