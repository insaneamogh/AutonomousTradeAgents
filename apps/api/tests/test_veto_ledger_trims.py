"""Trim aggregation for the Refusal Ledger.

A trim is a PARTIAL refusal: risk approved a smaller trade rather than
none. It lives on a row whose `risk_veto_rule` is NULL and whose
`risk_approved` is true, which is exactly why it cannot ride in the veto
query and why the two counts must never be summed.
"""

from __future__ import annotations

from app.services.council.ghost_service import count_trim_rules


def test_counts_each_rule_across_rows() -> None:
    rows = [
        {"risk_trim_rules": ["max_premium_pct_trim"]},
        {"risk_trim_rules": ["max_premium_pct_trim"]},
        {"risk_trim_rules": ["max_position_pct_trim"]},
    ]
    assert [(t.rule, t.count) for t in count_trim_rules(rows)] == [
        ("max_premium_pct_trim", 2),
        ("max_position_pct_trim", 1),
    ]


def test_one_row_can_carry_two_trims() -> None:
    rows = [{"risk_trim_rules": ["max_position_pct_trim", "short_unbounded_loss_cap_trim"]}]
    assert {t.rule: t.count for t in count_trim_rules(rows)} == {
        "max_position_pct_trim": 1,
        "short_unbounded_loss_cap_trim": 1,
    }


def test_ties_break_alphabetically_so_the_order_is_stable() -> None:
    """A scorecard that reshuffles between refreshes reads as broken."""
    rows = [{"risk_trim_rules": ["b_trim"]}, {"risk_trim_rules": ["a_trim"]}]
    assert [t.rule for t in count_trim_rules(rows)] == ["a_trim", "b_trim"]


def test_malformed_rows_are_skipped_not_raised() -> None:
    """Historical JSONB was written by several generations of this code.
    One bad row must not empty the whole ledger."""
    rows = [
        None,
        "not a dict",
        {},
        {"risk_trim_rules": None},
        {"risk_trim_rules": "max_premium_pct_trim"},  # string, not list
        {"risk_trim_rules": [None, "", 7, "max_premium_pct_trim"]},
    ]
    assert [(t.rule, t.count) for t in count_trim_rules(rows)] == [
        ("max_premium_pct_trim", 1)
    ]


def test_no_trims_is_an_empty_list_not_a_zero_row() -> None:
    assert count_trim_rules([{"risk_trim_rules": []}, {}]) == []
