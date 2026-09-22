"""`reasoning.feature_snapshot` + `reasoning.input_hash` on every decision.

The forecast ledger (docs/PLAN_PLATFORM.md Phase 3) scores every council
output against what actually happened, and a provider A/B asks whether two
models given the SAME inputs answer differently. Both need the exact inputs
each decision saw, stored with it. Macro, options context and fundamentals
all reach a prompt, and none of the three was persisted before 2026-09-23.
"""

from __future__ import annotations

from typing import Any

from trading_agents.runtime import _feature_snapshot, _input_hash, _reasoning_block


def _context() -> dict[str, Any]:
    return {
        "technicals": {"rsi_14": 58.2, "trend_regime": "up"},
        "quant": {"ret_63d_pct": 4.1},
        "macro": {"vix": 15.2, "dgs10": 4.1},
        "options_context": {"iv_rank": None, "days_to_earnings": None},
        "fundamentals": {"quality_score": 71},
        "feature_source": "alpaca",
        "last_price": 518.9,
        "bars": [1, 2, 3],  # bulky and derivable: deliberately NOT persisted
    }


def test_every_block_a_prompt_reads_is_persisted() -> None:
    snap = _feature_snapshot(_context())
    for block in ("technicals", "quant", "macro", "options_context", "fundamentals"):
        assert block in snap, f"{block} reaches a prompt but is not persisted"
    assert snap["feature_source"] == "alpaca"
    assert "bars" not in snap


def test_the_hash_ignores_key_order() -> None:
    a = {"macro": {"vix": 15.2, "dgs10": 4.1}, "last_price": 1.0}
    b = {"last_price": 1.0, "macro": {"dgs10": 4.1, "vix": 15.2}}
    assert _input_hash(a) == _input_hash(b)


def test_the_hash_changes_when_any_input_changes() -> None:
    base = _feature_snapshot(_context())
    moved = _feature_snapshot({**_context(), "macro": {"vix": 15.3, "dgs10": 4.1}})
    assert _input_hash(base) != _input_hash(moved)


def test_an_empty_snapshot_has_no_hash() -> None:
    """Empty snapshots would all collide by absence. That is the artefact
    that faked 48.6% redundancy in the 2026-09-21 cache measurement."""
    assert _input_hash({}) is None


def test_the_decision_row_carries_the_hash_of_its_own_snapshot() -> None:
    final: dict[str, Any] = {"context": _context()}
    block = _reasoning_block(final)  # type: ignore[arg-type]
    assert block["input_hash"] is not None
    assert block["input_hash"] == _input_hash(block["feature_snapshot"])
