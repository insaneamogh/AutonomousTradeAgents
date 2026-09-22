"""Forecast scorecard: every view the desk formed, scored on the tape."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tests.eval.forecast_scorecard import (
    View,
    calibration,
    disagreements,
    extract_views,
    independent,
    render,
    score,
)

from engine.prices.base import DailyClose


def _row(**reasoning) -> dict:
    return {
        "symbol": "NVDA",
        # 15:00 UTC on a Tuesday = 11:00 ET, the same ET date
        "triggered_at": datetime(2026, 9, 1, 15, 0, tzinfo=UTC),
        "final_action": "HOLD",
        "reasoning": reasoning,
    }


def _closes(start: date, prices: list[float]) -> list[DailyClose]:
    out, d = [], start
    for p in prices:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(DailyClose(day=d, close=p))
        d += timedelta(days=1)
    return out


def test_every_view_in_an_options_row_is_extracted() -> None:
    row = _row(
        strategy_fit={"winner": {"direction": "long", "score": 0.61}},
        options_resolution={
            "proceed": False, "direction": None, "conviction": None,
            "bull": {"direction": "long", "conviction": 0.55, "degraded": False},
            "bear": {"direction": "short", "conviction": 0.40, "degraded": False},
        },
        feature_snapshot={"last_price": 100.0},
    )
    views = extract_views(row)
    assert {(v.source, v.direction) for v in views} == {
        ("strategy_fit", "long"), ("bull", "long"), ("bear", "short"),
    }
    assert all(v.entry == 100.0 and v.day == date(2026, 9, 1) for v in views)


def test_a_degraded_agent_is_not_scored_as_an_opinion() -> None:
    row = _row(options_resolution={
        "proceed": False,
        "bull": {"direction": "long", "conviction": 0.5, "degraded": True},
        "bear": {"direction": "long", "conviction": 0.5, "degraded": False},
    })
    assert [v.source for v in extract_views(row)] == ["bear"]


def test_an_occ_symbol_is_scored_on_its_underlying() -> None:
    row = _row(strategy_fit={"winner": {"direction": "short"}})
    row["symbol"] = "NVDA260918C00225000"
    assert extract_views(row)[0].symbol == "NVDA"


def test_a_pre_migration_row_yields_nothing_rather_than_raising() -> None:
    assert extract_views({"symbol": "NVDA", "triggered_at": datetime(2026, 9, 1, tzinfo=UTC),
                          "reasoning": None, "final_action": "HOLD"}) == []


def test_score_is_sign_adjusted_and_uses_decision_time_price() -> None:
    closes = _closes(date(2026, 9, 1), [101.0, 102.0, 103.0, 104.0, 105.0, 110.0])
    long_view = View("bull", "NVDA", date(2026, 9, 1), "long", 0.5, 100.0)
    short_view = View("bear", "NVDA", date(2026, 9, 1), "short", 0.5, 100.0)
    assert score(long_view, closes, 5).ret_pct == 10.0
    assert score(short_view, closes, 5).ret_pct == -10.0


def test_a_view_the_tape_has_not_reached_is_not_scored() -> None:
    closes = _closes(date(2026, 9, 1), [100.0, 101.0])
    assert score(View("bull", "NVDA", date(2026, 9, 1), "long", 0.5, None), closes, 5) is None


def test_repeated_looks_at_one_name_count_once_per_window() -> None:
    """The live desk re-looked every few minutes. One move must not be
    counted dozens of times."""
    closes = _closes(date(2026, 9, 1), [100.0 + i for i in range(30)])
    views = [View("bull", "NVDA", date(2026, 9, 1), "long", 0.5, None) for _ in range(20)]
    scored = [score(v, closes, 5) for v in views]
    assert len(independent(scored, 5)) == 1


def test_calibration_buckets_by_stated_conviction() -> None:
    closes = _closes(date(2026, 9, 1), [100.0 + i for i in range(10)])
    hi = score(View("bull", "A", date(2026, 9, 1), "long", 0.61, None), closes, 5)
    lo = score(View("bull", "B", date(2026, 9, 1), "short", 0.35, None), closes, 5)
    cal = dict(calibration([hi, lo], "bull"))
    assert cal["0.6-1.0"].hit == 1.0
    assert cal["0.0-0.4"].hit == 0.0


def test_disagreements_credit_whichever_side_was_right() -> None:
    closes = _closes(date(2026, 9, 1), [100.0 + i for i in range(10)])
    bull = score(View("bull", "NVDA", date(2026, 9, 1), "long", 0.5, None), closes, 5)
    bear = score(View("bear", "NVDA", date(2026, 9, 1), "short", 0.5, None), closes, 5)
    assert disagreements([bull, bear]) == (1, 0, 1)


def test_render_never_raises_on_empty_sources() -> None:
    closes = _closes(date(2026, 9, 1), [100.0 + i for i in range(10)])
    only = [score(View("bull", "NVDA", date(2026, 9, 1), "long", 0.5, None), closes, 5)]
    text = render(only, 5)
    assert "bull" in text and "strategy_fit" in text
