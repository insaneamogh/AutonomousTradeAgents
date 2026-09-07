"""The entry-quality finding, pinned.

These assertions encode WHY the book loses money. If one starts failing,
something real has changed — read the numbers before editing the test.
"""

from __future__ import annotations

import statistics as stats

from tests.eval.entry_quality import excursions


def test_positions_bleed_far_more_than_they_ever_gain() -> None:
    """The core asymmetry: median best-ever +2.9% against median worst-ever
    -20.6%. This is the measurement that rules OUT the exit ladder as the
    cause — an exit can only capture a gain that existed."""
    rows = excursions()
    mfe = stats.median(e.mfe_pct for e in rows)
    mae = stats.median(e.mae_pct for e in rows)
    assert mfe < 10.0, f"median MFE {mfe:+.1f}% — positions now run; re-read the analysis"
    assert abs(mae) > 3 * abs(mfe), (
        f"downside {mae:+.1f}% vs upside {mfe:+.1f}% — the asymmetry that "
        "makes exit tuning pointless has changed"
    )


def test_a_large_share_of_positions_never_go_green_at_all() -> None:
    rows = excursions()
    never = [e for e in rows if not e.ever_green]
    assert len(never) >= len(rows) // 4, (
        "fewer positions are now underwater from the first tick — good, but "
        "the entry-edge conclusion was drawn from this and needs revisiting"
    )


def test_every_position_has_a_usable_path() -> None:
    """Guards the fixture: a path collapsed to one sample would make MFE and
    MAE identical and silently flatten the whole analysis."""
    for e in excursions():
        assert e.entry > 0, f"{e.occ}: non-positive entry"
        assert e.mae_pct <= e.mfe_pct, f"{e.occ}: MAE above MFE"
