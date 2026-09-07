"""Tests for the exit-ladder replay.

The headline test is `test_reproduces_the_three_real_closes`. A backtest
that cannot recover what actually happened has no standing to recommend
what should happen instead, so that assertion gates the sweep in `main()`
too — it refuses to print recommendations when acceptance fails.
"""

from __future__ import annotations

from tests.eval.exit_replay import (
    CURRENT,
    REAL_CLOSES,
    ContractPath,
    Ladder,
    load_paths,
    mark_to_last,
    replay,
    summarise,
    verify,
)


def _path(prices: list[float], entry: float = 10.0, qty: int = 1) -> ContractPath:
    return ContractPath(
        occ="TEST261016C00100000", entry=entry, qty=qty, multiplier=100,
        samples=tuple((i, p) for i, p in enumerate(prices)),
    )


# ── acceptance ────────────────────────────────────────────────────────


def test_reproduces_the_three_real_closes() -> None:
    """AAPL -536 / XLE -610 / NVDA +590, by the same exit reason each
    really fired. This is the whole basis for trusting the sweep."""
    assert verify() is True


def test_the_fixture_covers_every_position_with_a_known_outcome() -> None:
    occs = {p.occ for p in load_paths()}
    missing = set(REAL_CLOSES) - occs
    assert not missing, f"ground-truth contracts absent from the fixture: {missing}"


# ── censoring, the thing most likely to make this lie ─────────────────


def test_a_path_that_never_triggers_is_censored_not_a_win() -> None:
    """The bug this guards: treating the last observed price as an exit.
    That would silently score every still-open position at whatever the
    mark happened to be — which is exactly what makes a wide stop look
    free, since the censored set is disproportionately open losers."""
    r = replay(_path([10.0, 10.5, 10.2, 10.4]), CURRENT)
    assert r.exited is False
    assert r.reason is None
    assert r.pl_pct is None and r.pnl_usd is None


def test_censored_paths_are_excluded_from_expectancy() -> None:
    flat = _path([10.0, 10.1, 10.0])
    s = summarise([replay(flat, CURRENT)], CURRENT)
    assert s.exits == 0
    assert s.censored == 1
    assert s.expectancy_pct is None, "no completed trade means no expectancy"


def test_mark_to_last_does_include_censored_paths() -> None:
    """The other bound. Censored dropped flatters a wide stop; censored
    marked-to-last penalises it. Reporting both is what makes the
    conclusion independent of the bias."""
    p = _path([10.0, 9.0])          # -10%, never hits the -40% stop
    r = replay(p, CURRENT)
    assert r.exited is False
    assert mark_to_last([r], [p], CURRENT) == -10.0


# ── the ladder state machine ──────────────────────────────────────────


def test_stop_fires_on_the_downside() -> None:
    r = replay(_path([10.0, 8.0, 5.9]), CURRENT)   # -41%
    assert r.exited and r.reason == "option_stop_loss"
    assert r.pl_pct is not None and r.pl_pct <= -CURRENT.stop_loss_pct


def test_trail_fires_only_after_arming() -> None:
    """Up 20% then back to flat must NOT close: the trail arms at +35%."""
    assert replay(_path([10.0, 12.0, 10.0]), CURRENT).exited is False
    # Up 60% then giving back 30% of that peak does close.
    r = replay(_path([10.0, 16.0, 11.1]), CURRENT)
    assert r.exited and r.reason == "option_trail_stop"


def test_breakeven_win_rate_is_geometry_only() -> None:
    """-40 stop against a +24.5 minimum trailed win = 62%. This is the
    number the whole Phase 2 investigation turns on, so it is pinned."""
    assert round(CURRENT.min_trailed_win_pct, 2) == 24.5
    assert round(CURRENT.breakeven_win_rate_pct, 1) == 62.0
    tighter = Ladder(30.0, 50.0, 25.0, 150.0)
    assert round(tighter.breakeven_win_rate_pct, 1) == 44.4


# ── the finding itself ────────────────────────────────────────────────


def test_no_ladder_in_the_sweep_turns_this_book_profitable() -> None:
    """The result that cancelled Phase 2.1. Tightening the stop was the
    plan; the replay refutes it in BOTH bounds, and no configuration is
    even close to positive. The problem is entry quality, not exits.

    If this ever fails, something has genuinely improved — re-read the
    sweep before deleting the test.
    """
    paths = load_paths()
    for stop in (25.0, 30.0, 35.0, 40.0):
        lad = Ladder(stop, 35.0, 30.0, 150.0)
        results = [replay(p, lad) for p in paths]
        done = summarise(results, lad).expectancy_pct
        allp = mark_to_last(results, paths, lad)
        assert done is not None and done < 0, f"{lad}: completed-only {done}"
        assert allp is not None and allp < 0, f"{lad}: mark-to-last {allp}"


def test_tightening_the_stop_did_not_help_on_the_recorded_data() -> None:
    """Explicitly pins the refutation of the original hypothesis
    (stop 40 -> 30). A tighter stop converts censored positions into
    realised losses faster than it saves anything."""
    paths = load_paths()

    def exp(stop: float) -> float:
        lad = Ladder(stop, 35.0, 30.0, 150.0)
        return summarise([replay(p, lad) for p in paths], lad).expectancy_pct or 0.0

    assert exp(30.0) < exp(40.0), (
        "the recorded paths say a -30% stop is WORSE than the live -40%; "
        "do not ship the tightening hypothesis without new evidence"
    )
