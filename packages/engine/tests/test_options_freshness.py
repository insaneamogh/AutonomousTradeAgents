"""Option quote freshness — the gate that stops us selecting on stale greeks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from engine.options.freshness import MAX_QUOTE_AGE_SECONDS, quote_freshness

NOW = datetime(2026, 9, 2, 15, 0, 0, tzinfo=UTC)


def test_a_fresh_quote_passes() -> None:
    verdict = quote_freshness(quote_ts=NOW - timedelta(seconds=30), now=NOW)
    assert verdict.ok
    assert verdict.reason is None
    assert verdict.age_seconds == 30.0


def test_an_old_quote_is_refused_with_a_named_reason() -> None:
    verdict = quote_freshness(quote_ts=NOW - timedelta(seconds=901), now=NOW,
                              max_age_seconds=300.0)
    assert not verdict.ok
    assert verdict.reason == "stale_quote", "refusals must be NAMED for the ledger"
    assert "901" in verdict.detail


def test_an_absent_timestamp_is_refused_not_assumed_fresh() -> None:
    """The one place this codebase fails CLOSED rather than open.

    Everywhere else — the pre-flights, the CLI wrapper — a failure
    degrades to a slower path. Here it would degrade to a WRONG PRICE: an
    unknown age is exactly the case where a stale quote is most likely and
    least detectable, and "we could not tell how old it was" is not a
    reason to size a position on it.
    """
    verdict = quote_freshness(quote_ts=None, now=NOW)
    assert not verdict.ok
    assert verdict.reason == "stale_quote"
    assert "no timestamp" in verdict.detail


def test_a_future_timestamp_is_refused_as_clock_skew() -> None:
    """A quote stamped in the future means our clock and the venue's
    disagree — so no age derived from them is trustworthy in EITHER
    direction, including one that happens to look fresh."""
    verdict = quote_freshness(quote_ts=NOW + timedelta(seconds=45), now=NOW)
    assert not verdict.ok
    assert verdict.reason == "stale_quote"
    assert "FUTURE" in verdict.detail


def test_the_boundary_is_inclusive_of_the_limit() -> None:
    assert quote_freshness(
        quote_ts=NOW - timedelta(seconds=300), now=NOW, max_age_seconds=300.0
    ).ok
    assert not quote_freshness(
        quote_ts=NOW - timedelta(seconds=301), now=NOW, max_age_seconds=300.0
    ).ok


def test_the_indicative_feed_delay_is_why_the_default_cannot_be_tight() -> None:
    """Alpaca's default options feed is INDICATIVE: derived quotes on a
    documented ~15-minute delay. Every quote is then ~900s old as a
    PROPERTY OF THE TIER, not a fault.

    This pins the consequence: the module default (300s, sized for a
    real-time OPRA feed) refuses a normal indicative quote outright. That
    is why `RiskCaps.options_max_quote_age_seconds` ships at 0 (off) and
    why enabling it without checking the feed would stop all options
    trading.
    """
    indicative_age = timedelta(minutes=15)
    assert not quote_freshness(
        quote_ts=NOW - indicative_age, now=NOW, max_age_seconds=MAX_QUOTE_AGE_SECONDS
    ).ok
    # 1800 is the setting that passes the baseline delay while still
    # catching a contract that genuinely has not quoted in half an hour.
    assert quote_freshness(
        quote_ts=NOW - indicative_age, now=NOW, max_age_seconds=1800.0
    ).ok
