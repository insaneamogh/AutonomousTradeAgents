"""Contract funnel aggregation — apps/api/app/services/council/funnel_service.py.

``contract_funnel`` rides in ``agent_decisions.reasoning`` (JSONB) and has
been through several generations of shape. These tests pin the tolerance
rules from docs/IMPL_CONTRACT_FUNNEL_UI.md §1.1: a row missing the block
entirely is skipped rather than raising, a missing stage key means ABSENT
(not zero), ``dropped`` never goes negative, and the rejection stage is
the FIRST zero in fixed evaluation order, not the last.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.council.funnel_service import (
    build_funnel_report_from_rows,
)

_WHEN = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)


def _row(
    *,
    decision_id: str = "11111111-1111-1111-1111-111111111111",
    symbol: str = "NVDA",
    final_action: str = "HOLD",
    reasoning: object,
) -> dict[str, Any]:
    return {
        "id": decision_id,
        "symbol": symbol,
        "triggered_at": _WHEN,
        "final_action": final_action,
        "reasoning": reasoning,
    }


def _funnel(counts: dict[str, int], *, rejection_reason: str | None = None, selected_occ: str | None = None) -> dict[str, Any]:
    return {
        "contract_funnel": {
            "counts": counts,
            "rejection_reason": rejection_reason,
            "selected_occ": selected_occ,
        }
    }


# ─────────────────────────────────────────────────────────────────────
# test_aggregates_stages_across_runs
# ─────────────────────────────────────────────────────────────────────


def test_aggregates_stages_across_runs() -> None:
    """Break this by summing the wrong axis (e.g. last-write-wins instead
    of accumulating across runs) — SUM per stage, across ALL runs."""
    rows = [
        _row(
            decision_id="a",
            final_action="BUY",
            reasoning=_funnel(
                {
                    "total": 100,
                    "contract_type": 50,
                    "dte_window": 40,
                    "delta_band": 10,
                    "liquidity": 3,
                    "iv_present": 3,
                    "iv_realized_vol_band": 1,
                },
                selected_occ="NVDA260918C00225000",
            ),
        ),
        _row(
            decision_id="b",
            final_action="HOLD",
            reasoning=_funnel(
                {
                    "total": 50,
                    "contract_type": 20,
                    "dte_window": 15,
                    "delta_band": 5,
                    "liquidity": 2,
                    "iv_present": 2,
                    "iv_realized_vol_band": 0,
                },
                rejection_reason="iv_outside_plausible_band",
            ),
        ),
    ]

    report = build_funnel_report_from_rows(rows, window_days=30, limit=20)

    by_key = {s.key: (s.survivors, s.dropped) for s in report.aggregate.stages}
    assert by_key == {
        "total": (150, 0),
        "contract_type": (70, 80),
        "dte_window": (55, 15),
        "delta_band": (15, 40),
        "liquidity": (5, 10),
        "iv_present": (5, 0),
        "iv_realized_vol_band": (1, 4),
    }
    assert report.aggregate.runs == 2
    assert report.aggregate.bought == 1


def test_stage_order_matches_fixed_evaluation_order() -> None:
    rows = [
        _row(
            reasoning=_funnel(
                {
                    "total": 10,
                    "contract_type": 5,
                    "dte_window": 5,
                    "delta_band": 5,
                    "liquidity": 5,
                    "iv_present": 5,
                    "iv_realized_vol_band": 5,
                }
            )
        )
    ]
    report = build_funnel_report_from_rows(rows, window_days=30)
    assert [s.key for s in report.aggregate.stages] == [
        "total",
        "contract_type",
        "dte_window",
        "delta_band",
        "liquidity",
        "iv_present",
        "iv_realized_vol_band",
    ]


# ─────────────────────────────────────────────────────────────────────
# test_row_without_contract_funnel_is_skipped
# ─────────────────────────────────────────────────────────────────────


def test_row_without_contract_funnel_is_skipped() -> None:
    """Break this by indexing `reasoning['contract_funnel']` directly
    instead of `.get(...)` — must not KeyError, must not be counted."""
    rows = [
        _row(reasoning={"strategy_fit": {"score": 10}}),  # no contract_funnel key
        _row(reasoning={}),  # empty reasoning dict
    ]
    report = build_funnel_report_from_rows(rows, window_days=30)
    assert report.aggregate.runs == 0
    assert report.aggregate.stages == []
    assert report.recent == []


# ─────────────────────────────────────────────────────────────────────
# test_non_dict_reasoning_does_not_raise
# ─────────────────────────────────────────────────────────────────────


def test_non_dict_reasoning_does_not_raise() -> None:
    """Break this by removing the `isinstance(reasoning, dict)` guard —
    a bare `.get(...)` on a string/list/None raises AttributeError."""
    rows = [
        _row(reasoning="not a dict"),
        _row(reasoning=["also", "not", "a", "dict"]),
        _row(reasoning=None),
        _row(reasoning=7),
        # contract_funnel itself non-dict is the same tolerance rule.
        _row(reasoning={"contract_funnel": "not a dict either"}),
    ]
    report = build_funnel_report_from_rows(rows, window_days=30)
    assert report.aggregate.runs == 0
    assert report.recent == []


# ─────────────────────────────────────────────────────────────────────
# test_dropped_never_negative
# ─────────────────────────────────────────────────────────────────────


def test_dropped_never_negative() -> None:
    """Break this by computing `dropped = prev - survivors` with no floor
    — malformed data (a count that RISES between stages) must read as 0
    dropped, never negative."""
    rows = [
        _row(
            reasoning=_funnel(
                {
                    "total": 5,
                    "contract_type": 10,  # rose vs. total — malformed but must not go negative
                    "dte_window": 10,
                }
            )
        )
    ]
    report = build_funnel_report_from_rows(rows, window_days=30)
    by_key = {s.key: s.dropped for s in report.aggregate.stages}
    assert by_key["contract_type"] == 0
    assert by_key["dte_window"] == 0
    assert all(d >= 0 for d in by_key.values())


def test_missing_stage_key_is_absent_not_zero() -> None:
    """A stage no run in the window reports must not appear as a
    fabricated zero-survivor stage."""
    rows = [_row(reasoning=_funnel({"total": 10, "contract_type": 4}))]
    report = build_funnel_report_from_rows(rows, window_days=30)
    keys = {s.key for s in report.aggregate.stages}
    assert keys == {"total", "contract_type"}
    assert "dte_window" not in keys


# ─────────────────────────────────────────────────────────────────────
# test_rejection_stage_is_the_first_zero
# ─────────────────────────────────────────────────────────────────────


def test_rejection_stage_is_the_first_zero() -> None:
    """Break this by reporting the LAST zero stage instead of the first
    (e.g. iterating in reverse, or not breaking on first match)."""
    rows = [
        _row(
            reasoning=_funnel(
                {
                    "total": 10,
                    "contract_type": 10,
                    "dte_window": 0,  # first zero
                    "delta_band": 0,
                    "liquidity": 0,
                    "iv_present": 0,
                    "iv_realized_vol_band": 0,  # last zero
                },
                rejection_reason="no_expiry_in_window",
            )
        )
    ]
    report = build_funnel_report_from_rows(rows, window_days=30)
    assert len(report.recent) == 1
    assert report.recent[0].rejection_stage == "dte_window"


def test_rejection_stage_handles_no_candidates_at_all() -> None:
    """total=0 is select_contract's own "no_candidates" case — it isn't
    one of the six named `_STAGE_REJECTION_REASONS`, but it is still a
    stage hitting zero and must be nameable."""
    rows = [_row(reasoning=_funnel({"total": 0}, rejection_reason="no_candidates"))]
    report = build_funnel_report_from_rows(rows, window_days=30)
    assert report.recent[0].rejection_stage == "total"


def test_a_bought_run_has_no_rejection_stage() -> None:
    rows = [
        _row(
            final_action="BUY",
            reasoning=_funnel(
                {
                    "total": 10,
                    "contract_type": 5,
                    "dte_window": 4,
                    "delta_band": 2,
                    "liquidity": 1,
                    "iv_present": 1,
                    "iv_realized_vol_band": 1,
                },
                selected_occ="NVDA260918C00225000",
            ),
        )
    ]
    report = build_funnel_report_from_rows(rows, window_days=30)
    run = report.recent[0]
    assert run.rejection_stage is None
    assert run.outcome == "bought"
    assert run.selected_occ == "NVDA260918C00225000"


# ─────────────────────────────────────────────────────────────────────
# Outcome mapping + top rejection reasons (bonus coverage)
# ─────────────────────────────────────────────────────────────────────


def test_a_risk_vetoed_pass_is_held_not_bought() -> None:
    """A contract can survive contract selection and still not be
    bought — the sizer can floor to 0, or risk can veto afterward
    (final_action becomes VETOED). Both read as "held" here."""
    rows = [
        _row(
            final_action="VETOED",
            reasoning=_funnel(
                {"total": 10, "contract_type": 5, "dte_window": 4, "delta_band": 2, "liquidity": 1, "iv_present": 1, "iv_realized_vol_band": 1},
                selected_occ="NVDA260918C00225000",
            ),
        )
    ]
    report = build_funnel_report_from_rows(rows, window_days=30)
    assert report.recent[0].outcome == "held"
    assert report.aggregate.bought == 0


def test_top_rejection_reasons_sorted_by_count_desc() -> None:
    rows = [
        _row(decision_id="a", reasoning=_funnel({"total": 1, "contract_type": 0}, rejection_reason="no_matching_contract_type")),
        _row(decision_id="b", reasoning=_funnel({"total": 1, "contract_type": 0}, rejection_reason="no_matching_contract_type")),
        _row(decision_id="c", reasoning=_funnel({"total": 1, "contract_type": 1, "dte_window": 0}, rejection_reason="no_expiry_in_window")),
    ]
    report = build_funnel_report_from_rows(rows, window_days=30)
    assert report.aggregate.top_rejection_reasons == [
        {"reason": "no_matching_contract_type", "count": 2},
        {"reason": "no_expiry_in_window", "count": 1},
    ]


def test_recent_is_capped_at_limit_but_aggregate_is_not() -> None:
    rows = [
        _row(decision_id=str(i), reasoning=_funnel({"total": 1}))
        for i in range(5)
    ]
    report = build_funnel_report_from_rows(rows, window_days=30, limit=2)
    assert len(report.recent) == 2
    assert report.aggregate.runs == 5


# ─────────────────────────────────────────────────────────────────────
# test_scoped_to_the_caller
# ─────────────────────────────────────────────────────────────────────


class _CapturingSession:
    """Records every statement executed; returns no rows. Mirrors the
    identical helper in apps/api/tests/test_tenant_isolation.py, which
    pins the same contract for ghost_service's builders."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self) -> _CapturingSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)

        class _Result:
            def all(self) -> list[Any]:
                return []

        return _Result()


def _compiled(session: _CapturingSession) -> str:
    return " ".join(str(s) for s in session.statements)


def test_scoped_to_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break this by dropping the `*tenant` filter from the query — the
    emitted SQL must constrain agent_decisions.user_id."""
    import anyio

    from app.services.council import funnel_service

    session = _CapturingSession()
    monkeypatch.setattr(funnel_service, "async_session_factory", lambda: lambda: session)

    anyio.run(
        lambda: funnel_service.build_funnel_report(
            30, user_id="11111111-1111-1111-1111-111111111111"
        )
    )

    sql = _compiled(session)
    assert "agent_decisions.user_id" in sql, "build_funnel_report emitted an unscoped query"


def test_unknown_tenant_returns_empty_without_reaching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed user id must degrade to "no rows", never to "every row"."""
    import anyio

    from app.services.council import funnel_service

    session = _CapturingSession()
    monkeypatch.setattr(funnel_service, "async_session_factory", lambda: lambda: session)

    report = anyio.run(
        lambda: funnel_service.build_funnel_report(30, user_id="not-a-uuid")
    )

    assert report.aggregate.runs == 0
    assert report.recent == []
    assert session.statements == [], "an unknown tenant must not reach the database"
