"""Analysts score the TRADE in its proposed direction, not the chart.

`min_specialist_avg_score` averages analyst scores as "how well does this
support the trade". The analysts were never told the direction, so a
correct bearish read on a put or short setup came back LOW and pulled the
average under the floor: a right answer scored as a refusal. The macro
prompt was also written for longs only, and compared FRED's broad dollar
index (about 120) with the ICE DXY's 105, so "strong dollar" was always on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from trading_agents.llm import LLMResponse
from trading_agents.nodes.macro_analyst import macro_analyst_node
from trading_agents.nodes.technical_analyst import technical_analyst_node
from trading_agents.prompts import MACRO_ANALYST


class _RecordingLLM:
    def __init__(self) -> None:
        self.users: list[str] = []

    async def complete(self, *, system: str, user: str, **_: Any) -> LLMResponse:
        self.users.append(user)
        body = {"score": 60, "confidence": 0.5, "thesis": "ok", "citations": []}
        return LLMResponse(text=json.dumps(body), model="test")


def _state(direction: str | None) -> dict[str, Any]:
    return {
        "symbol": "NVDA",
        "horizon": "short",
        "selected_direction": direction,
        "context": {
            "technicals": {"rsi_14": 38.0},
            "macro": {"vix_level": 16.0, "dxy_index": 121.4, "dxy_zscore_1y": 0.2},
        },
    }


@pytest.mark.parametrize("node", [technical_analyst_node, macro_analyst_node])
@pytest.mark.parametrize("direction", ["long", "short"])
async def test_each_analyst_is_told_the_direction_it_is_scoring(node, direction: str) -> None:
    llm = _RecordingLLM()
    await node(_state(direction), llm)  # type: ignore[arg-type]
    assert f"Proposed direction: {direction}" in llm.users[0]


@pytest.mark.parametrize("node", [technical_analyst_node, macro_analyst_node])
async def test_a_missing_direction_says_so_rather_than_guessing(node) -> None:
    llm = _RecordingLLM()
    await node(_state(None), llm)  # type: ignore[arg-type]
    assert "Proposed direction: unspecified" in llm.users[0]


async def test_the_macro_analyst_sees_the_dollar_zscore_not_the_level() -> None:
    llm = _RecordingLLM()
    await macro_analyst_node(_state("long"), llm)  # type: ignore[arg-type]
    assert "dxy_zscore_1y" in llm.users[0]
    assert "dxy_index" not in llm.users[0]


def test_the_macro_prompt_has_no_fixed_dollar_level_and_is_not_long_only() -> None:
    assert "105" not in MACRO_ANALYST
    assert "SUPPORTS or HINDERS a long position" not in MACRO_ANALYST
    assert "PROPOSED TRADE" in MACRO_ANALYST
