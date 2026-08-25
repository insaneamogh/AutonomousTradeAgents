"""Node output guardrails — LLM numbers are clamped before they mean anything.

The load-bearing test here is ``test_score_900_cannot_defeat_specialist_avg_veto``:
an analyst that returns ``score: 900`` (hallucinated, or steered there by an
injected ticker) must not be able to drag the council average past the
``min_specialist_avg_score`` floor. Everything else guards the crash paths —
a model that answers ``risk_level: "high"`` used to take the whole run down
with a ValueError.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from engine.risk.rules.specialist_avg_score import min_specialist_avg_score
from engine.risk.types import RiskCaps, SpecialistScore
from trading_agents.llm import LLMResponse, Model
from trading_agents.nodes._guards import clamp_confidence, clamp_level, clamp_score
from trading_agents.nodes.drafter import drafter_node
from trading_agents.nodes.fundamental_analyst import fundamental_analyst_node
from trading_agents.nodes.macro_analyst import macro_analyst_node
from trading_agents.nodes.risk_officer import _specialists_from_state
from trading_agents.nodes.technical_analyst import technical_analyst_node


class ScriptedLLM:
    """LLM stand-in that replays one canned JSON body per system prompt.

    Keyed on a substring of the system prompt so a single instance can
    answer for several council nodes in one pass.
    """

    mock = True

    def __init__(self, by_role: dict[str, dict[str, Any]], default: dict[str, Any]) -> None:
        self._by_role = by_role
        self._default = default

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str = Model.SONNET,
        max_tokens: int = 800,
        cache_system: bool = True,
    ) -> LLMResponse:
        body = self._default
        for needle, payload in self._by_role.items():
            if needle.lower() in system.lower():
                body = payload
                break
        return LLMResponse(text=json.dumps(body), model=model)


def _analyst(score: Any, confidence: Any = 0.9) -> dict[str, Any]:
    return {"score": score, "confidence": confidence, "thesis": "t", "citations": []}


def _state(**over: Any) -> Any:
    base: dict[str, Any] = {
        "symbol": "NVDA",
        "horizon": "short",
        "context": {"technicals": {}, "fundamentals": {}, "macro": {}},
    }
    base.update(over)
    return base


# ─────────────────────────────────────────────────────────────────────
# The attack: an out-of-range score must not buy its way past the veto
# ─────────────────────────────────────────────────────────────────────


async def test_score_900_cannot_defeat_specialist_avg_veto() -> None:
    """One inflated analyst score must not lift the council average.

    Unclamped, (900 + 10 + 10) / 3 = 306.7 clears the 45.0 floor and the
    trade sails through on a single bogus number. Clamped, the same run
    averages (100 + 10 + 10) / 3 = 40.0 and the veto fires.
    """
    llm = ScriptedLLM(
        by_role={
            "technical": _analyst(900),
            "fundamental": _analyst(10),
            "macro": _analyst(10),
        },
        default=_analyst(10),
    )

    state = _state()
    state = await technical_analyst_node(state, llm)
    state = await fundamental_analyst_node(state, llm)
    state = await macro_analyst_node(state, llm)

    # The clamp happened at the node boundary, before state was written.
    assert state["technical"]["score"] == 100.0

    specialists = _specialists_from_state(state)
    assert [s.score for s in specialists] == [100.0, 10.0, 10.0]

    avg = sum(s.score for s in specialists) / len(specialists)
    caps = RiskCaps()
    assert avg < caps.min_specialist_avg_score, "clamped average must sit under the floor"

    decision = min_specialist_avg_score(
        proposal=None,  # type: ignore[arg-type] — rule only reads `specialists`
        context=None,  # type: ignore[arg-type]
        caps=caps,
        specialists=specialists,
    )
    assert decision is not None, "veto must still fire"
    assert decision.approved is False
    assert decision.veto_rule == "min_specialist_avg_score"

    # Pin the counterfactual so this stays a regression test: fed the raw
    # scores the model actually returned, the same rule waves it through.
    unclamped = [
        SpecialistScore(name=s.name, score=raw, confidence=s.confidence)
        for s, raw in zip(specialists, (900.0, 10.0, 10.0), strict=True)
    ]
    assert (
        min_specialist_avg_score(
            proposal=None,  # type: ignore[arg-type]
            context=None,  # type: ignore[arg-type]
            caps=caps,
            specialists=unclamped,
        )
        is None
    ), "the unclamped score is exactly what the clamp exists to stop"


async def test_analyst_scores_and_confidence_are_bounded() -> None:
    """Every out-of-range shape the model can emit lands back in range."""
    llm = ScriptedLLM(by_role={}, default=_analyst(-250, confidence=7.5))

    for node in (technical_analyst_node, fundamental_analyst_node, macro_analyst_node):
        state = await node(_state(), llm)
        key = {
            technical_analyst_node: "technical",
            fundamental_analyst_node: "fundamental",
            macro_analyst_node: "macro",
        }[node]
        assert state[key]["score"] == 0.0
        assert state[key]["confidence"] == 1.0


# ─────────────────────────────────────────────────────────────────────
# Drafter — word-form ordinals used to crash the run
# ─────────────────────────────────────────────────────────────────────


def _drafter_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "verdict": "BUY",
        "confidence": 0.7,
        "rationale": "r",
        "bull_case": "b",
        "bear_case": "be",
        "risk_level": 3,
        "conviction_level": 3,
    }
    body.update(over)
    return body


async def test_drafter_survives_word_form_risk_level() -> None:
    """``risk_level: "high"`` must not raise — it used to kill the run."""
    llm = ScriptedLLM(by_role={}, default=_drafter_body(risk_level="high"))
    state = _state(
        selected_strategy="momentum",
        context={"technicals": {"atr_14": 4.0}, "last_price": 100.0, "portfolio_equity": 100_000.0},
    )

    result = await drafter_node(state, llm)

    proposal = result["proposal"]
    assert proposal is not None, "a bad ordinal must not suppress the proposal"
    assert proposal["risk_level"] == 3  # neutral default, not a crash


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("high", 3),      # word form → default
        (3.5, 3),         # float → truncated, in range
        (99, 5),          # above scale → clamped
        (0, 1),           # below scale → clamped
        ("4", 4),         # numeric string → parsed
        (None, 3),        # missing → default
    ],
)
async def test_drafter_ordinals_are_coerced(raw: Any, expected: int) -> None:
    llm = ScriptedLLM(by_role={}, default=_drafter_body(conviction_level=raw))
    state = _state(
        selected_strategy="momentum",
        context={"technicals": {"atr_14": 4.0}, "last_price": 100.0, "portfolio_equity": 100_000.0},
    )

    result = await drafter_node(state, llm)
    assert result["proposal"]["conviction_level"] == expected


# ─────────────────────────────────────────────────────────────────────
# The helpers themselves
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(900, 100.0), (-1, 0.0), (50, 50.0), ("72.5", 72.5), ("high", 50.0), (None, 50.0)],
)
def test_clamp_score(raw: Any, expected: float) -> None:
    assert clamp_score(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(7.5, 1.0), (-0.2, 0.0), (0.4, 0.4), ("bad", 0.0), (None, 0.0)],
)
def test_clamp_confidence(raw: Any, expected: float) -> None:
    assert clamp_confidence(raw) == expected


def test_clamps_reject_non_finite_and_bools() -> None:
    """NaN/inf survive float() and poison every downstream average."""
    assert clamp_score(float("nan")) == 50.0
    assert clamp_score(float("inf")) == 50.0
    assert clamp_confidence(float("-inf")) == 0.0
    # bool is an int subclass — a True score is a bug, not a 1.0.
    assert clamp_score(True) == 50.0
    assert clamp_level(True, field="risk_level") == 3
