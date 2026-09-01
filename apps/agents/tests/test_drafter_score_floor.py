"""The Drafter's specialist-score floor must be the ENGINE's floor.

CLAUDE.md §4.4, live: the prompt said "average score < 45 -> HOLD" as a
literal while `RISK_PROFILE=aggressive_paper` set
`min_specialist_avg_score` to 40.0. The risk engine honoured 40; the model
went on refusing at 45 one layer earlier, so lowering the cap could never
reach a single trade. Observed in production 2026-09-01: "Technical
analyst score of 38.0 falls below the 45-point hard floor".
"""

from __future__ import annotations

import pytest

from engine.risk import RiskCaps
from trading_agents.prompts.drafter import (
    DEFAULT_MIN_SPECIALIST_AVG_SCORE,
    drafter_prompt,
)


def test_the_prompt_carries_no_hardcoded_score_floor() -> None:
    """The literal is what made the two numbers drift. If one comes back,
    this fails even though the injected floor still renders correctly."""
    rendered = drafter_prompt(40.0)
    assert "score < 40 " in rendered
    assert "score < 45" not in rendered
    assert "__MIN_SPECIALIST_AVG_SCORE__" not in rendered


def test_the_aggressive_profiles_floor_reaches_the_prompt() -> None:
    caps = RiskCaps.aggressive_paper()
    assert caps.min_specialist_avg_score == pytest.approx(40.0)
    assert f"score < {caps.min_specialist_avg_score:g} " in drafter_prompt(
        caps.min_specialist_avg_score
    )


def test_the_conservative_profiles_floor_reaches_the_prompt() -> None:
    caps = RiskCaps()
    assert caps.min_specialist_avg_score == pytest.approx(45.0)
    assert "score < 45 " in drafter_prompt(caps.min_specialist_avg_score)


def test_no_caps_falls_back_to_the_stricter_floor_not_the_looser_one() -> None:
    """Failing toward MORE refusal is the direction that cannot lose money
    by accident — the same fail-closed contract `env_flag` uses."""
    assert DEFAULT_MIN_SPECIALIST_AVG_SCORE == pytest.approx(45.0)
    assert "score < 45 " in drafter_prompt(None)


def test_the_json_output_block_survives_substitution() -> None:
    """The template contains a literal JSON object. A `str.format`-based
    substitution would raise on its unescaped braces."""
    rendered = drafter_prompt(40.0)
    assert '"verdict": "BUY" | "SELL" | "HOLD"' in rendered
    assert '"conviction_level"' in rendered
