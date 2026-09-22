"""Pre-registered candidates: the rules, not the (slow) full run.

The full 6-year run lives in `python -m tests.eval.candidates`, and its one
result is recorded in that module's docstring. Both candidates FAIL.
"""

from __future__ import annotations

from tests.eval.candidates import REVERSAL_Z, Momentum12_1, ShortTermReversal


def test_reversal_calls_only_outside_the_band() -> None:
    m = ShortTermReversal()
    assert m.evaluate({"quant": {"price_zscore_20": -REVERSAL_Z - 0.1}}, symbol="X").direction == "long"
    assert m.evaluate({"quant": {"price_zscore_20": REVERSAL_Z + 0.1}}, symbol="X").direction == "short"
    inside = m.evaluate({"quant": {"price_zscore_20": 1.0}}, symbol="X")
    assert inside.direction == "flat" and not inside.meta.get("strategy_id")


def test_momentum_excludes_the_latest_month() -> None:
    """+10% over 12 months, all of it in the last month, is ~0 on 12-1: the
    construction skips the most recent month, which tends to REVERSE."""
    m = Momentum12_1()
    s = m.evaluate({"quant": {"ret_252d_pct": 10.0, "ret_21d_pct": 10.0}}, symbol="X")
    assert s.meta.get("strategy_id") is None or abs(s.meta["m"]) < 1e-9
    up = m.evaluate({"quant": {"ret_252d_pct": 30.0, "ret_21d_pct": -5.0}}, symbol="X")
    assert up.direction == "long"


def test_missing_inputs_abstain_rather_than_call_flat() -> None:
    assert ShortTermReversal().evaluate({"quant": {}}, symbol="X").abstained
    assert Momentum12_1().evaluate({"quant": {"ret_252d_pct": 5.0}}, symbol="X").abstained
