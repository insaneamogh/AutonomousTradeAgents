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


# ─────────────────────────────────────────────────────────────────────
# equity=0.0 / last_price=0.0 must NOT collapse to the fixture defaults
#
# `ctx.get("portfolio_equity", 100_000.0) or 100_000.0` treats a
# genuinely-zero value the same as an absent one (0.0 is falsy in
# Python), so a fully-drawn-down account would silently size a NEW
# trade against the fake $100k fixture instead of refusing. Same bug,
# same fix, in risk_officer.py's mock-provider equity read.
# ─────────────────────────────────────────────────────────────────────


async def test_zero_equity_reaches_the_sizer_as_zero_not_the_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        drafter_mod,
        "complete_json",
        AsyncMock(
            return_value=(
                {
                    "verdict": "BUY",
                    "confidence": 0.6,
                    "rationale": "Clean setup.",
                    "bull_case": "Bull.",
                    "bear_case": "Bear.",
                    "risk_level": 3,
                    "conviction_level": 3,
                },
                False,
            )
        ),
    )
    state = _state(context={"last_price": 200.0, "portfolio_equity": 0.0, "technicals": {}})

    out = await drafter_mod.drafter_node(state, llm=object())

    # atr_position_size's own account_equity<=0 branch, not the
    # confidence=0 branch and not a trade sized against $100k.
    assert out["final_action"] == "HOLD"
    assert "non-positive price or equity" in out["drafter_rationale"]


async def test_zero_last_price_reaches_the_sizer_as_zero_not_the_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        drafter_mod,
        "complete_json",
        AsyncMock(
            return_value=(
                {
                    "verdict": "BUY",
                    "confidence": 0.6,
                    "rationale": "Clean setup.",
                    "bull_case": "Bull.",
                    "bear_case": "Bear.",
                    "risk_level": 3,
                    "conviction_level": 3,
                },
                False,
            )
        ),
    )
    state = _state(context={"last_price": 0.0, "portfolio_equity": 100_000.0, "technicals": {}})

    out = await drafter_mod.drafter_node(state, llm=object())

    assert out["final_action"] == "HOLD"
    assert "non-positive price or equity" in out["drafter_rationale"]


def test_risk_officer_mock_provider_does_not_collapse_zero_equity() -> None:
    from trading_agents.nodes.risk_officer import _default_provider

    provider = _default_provider({"context": {"portfolio_equity": 0.0}})
    assert provider.account_equity == 0.0
    assert provider.cash == 0.0
    assert provider.buying_power == 0.0
