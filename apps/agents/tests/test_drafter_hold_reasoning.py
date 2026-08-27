"""A HOLD verdict used to discard the model's own explanation.

The DRAFTER prompt asks the model to explain a HOLD in the bear case
even though it's declining to draft — ``drafter_node`` threw that answer
away on every HOLD path, so the audit row and the theater UI showed a
bare "No proposal — HOLD." even when the model had written three
sentences about why. These pin that the explanation survives.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from trading_agents.nodes import drafter as drafter_mod
from trading_agents.progress import summarize_node


def _state(**overrides: object) -> dict:
    base = {
        "symbol": "NVDA",
        "selected_strategy": "momentum",
        "selected_direction": "long",
        "selector_confidence": 0.7,
        "selector_rationale": "momentum_long:trailing_3m_return",
        "horizon": "short",
        "regime": "choppy",
        "context": {"last_price": 200.0, "portfolio_equity": 100_000.0, "technicals": {}},
        "technical": {"score": 55, "confidence": 0.5, "thesis": "mixed signal"},
    }
    base.update(overrides)
    return base


async def test_model_hold_verdict_keeps_its_own_bull_bear_and_rationale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        drafter_mod,
        "complete_json",
        AsyncMock(
            return_value=(
                {
                    "verdict": "HOLD",
                    "confidence": 0.4,
                    "rationale": "Setup is real but conviction is too thin to size.",
                    "bull_case": "Momentum is intact and the trend is up.",
                    "bear_case": "Volume is unconfirmed and RSI is stretched.",
                    "risk_level": 2,
                    "conviction_level": 2,
                },
                False,
            )
        ),
    )

    out = await drafter_mod.drafter_node(_state(), llm=object())

    assert out["final_action"] == "HOLD"
    assert out["proposal"] is None
    assert "too thin to size" in out["drafter_rationale"]
    assert "Momentum is intact" in out["bull_case"]
    assert "unconfirmed" in out["bear_case"]

    # The theater card must show the SAME explanation, not the generic
    # "Sizes the order and writes the plan" placeholder.
    card = summarize_node("drafter", out)
    assert card is not None
    assert "too thin to size" in card["thesis"]


async def test_sizer_zeroed_qty_keeps_the_models_case_plus_the_sizers_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        drafter_mod,
        "complete_json",
        AsyncMock(
            return_value=(
                {
                    "verdict": "BUY",
                    # Zero confidence collapses the ATR sizer's qty to 0
                    # (atr_position_size's own confidence==0 branch) — the
                    # deterministic forced-HOLD path, distinct from the
                    # model's own HOLD verdict tested above.
                    "confidence": 0.0,
                    "rationale": "Clean breakout above resistance.",
                    "bull_case": "Strong relative strength versus the sector.",
                    "bear_case": "Thin liquidity could widen slippage.",
                    "risk_level": 3,
                    "conviction_level": 3,
                },
                False,
            )
        ),
    )

    out = await drafter_mod.drafter_node(_state(), llm=object())

    assert out["final_action"] == "HOLD"
    assert out["proposal"] is None
    assert "Sizer returned 0 shares" in out["drafter_rationale"]
    assert "Clean breakout" in out["drafter_rationale"]
    assert "Strong relative strength" in out["bull_case"]


async def test_parse_failure_hold_has_no_fabricated_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two upstream HOLDs (parse failure; no strategy fit) genuinely
    have nothing to explain — must not synthesize a fake rationale."""
    monkeypatch.setattr(
        drafter_mod, "complete_json", AsyncMock(return_value=(None, True))
    )

    out = await drafter_mod.drafter_node(_state(), llm=object())

    assert out["final_action"] == "HOLD"
    assert "drafter_rationale" not in out
    card = summarize_node("drafter", out)
    assert card is not None
    assert card["thesis"] == ""
