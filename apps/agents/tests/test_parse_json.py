"""`LLM.parse_json` — every council node's JSON goes through it.

Claude returns the object alone (sometimes fenced). GLM, the cheaper
provider this repo is moving to, routinely wraps it in prose. Before the
fix, that shape raised; `complete_json` paid for a re-ask, and a second
prose reply degraded the node to its neutral fallback. A FORMATTING habit
would then read in the ledger as a more defensive model.
"""

from __future__ import annotations

import pytest

from trading_agents.llm import LLM


def test_bare_object() -> None:
    assert LLM.parse_json('{"score": 62, "confidence": 0.5}') == {
        "score": 62,
        "confidence": 0.5,
    }


def test_fenced_object() -> None:
    assert LLM.parse_json('```json\n{"score": 62}\n```') == {"score": 62}


def test_object_wrapped_in_prose() -> None:
    """The GLM shape: a lead-in sentence, the object, a trailing note."""
    text = (
        "Here is my assessment of the setup.\n\n"
        '{"score": 38, "confidence": 0.6, "thesis": "below 50DMA"}\n\n'
        "Note: RSI is not yet oversold."
    )
    assert LLM.parse_json(text)["score"] == 38


def test_fenced_object_inside_prose() -> None:
    text = 'Analysis follows.\n```json\n{"verdict": "HOLD"}\n```\nDone.'
    assert LLM.parse_json(text) == {"verdict": "HOLD"}


def test_the_last_object_wins_when_reasoning_precedes_the_answer() -> None:
    """A model that reasons first may restate an input object before its
    verdict. The verdict comes last."""
    text = (
        'The input was {"rsi_14": 71}. Weighing that against the trend...\n'
        '{"score": 44, "confidence": 0.3}'
    )
    assert LLM.parse_json(text) == {"score": 44, "confidence": 0.3}


def test_nested_objects_are_returned_whole() -> None:
    text = 'Result: {"view": {"direction": "long", "conviction": 0.55}, "ok": true}'
    assert LLM.parse_json(text) == {
        "view": {"direction": "long", "conviction": 0.55},
        "ok": True,
    }


def test_braces_inside_strings_do_not_confuse_extraction() -> None:
    text = 'Sure. {"thesis": "breakout above {resistance}", "score": 70}'
    assert LLM.parse_json(text)["score"] == 70


def test_no_object_raises_so_the_re_ask_still_fires() -> None:
    """`complete_json` relies on a raise to trigger its one re-ask and the
    degraded flag. A parser that returned {} would silently turn every
    malformed reply into an empty, non-degraded result."""
    with pytest.raises(ValueError):
        LLM.parse_json("I would stand down here; the evidence is thin.")
