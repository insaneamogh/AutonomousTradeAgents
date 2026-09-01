"""Deterministic pre-pass + budget gate in ``daily_cron.main``.

2026-09-01 post-mortem: the baseline sweep had NO ceiling of its own on
how many watchlist symbols could clear ``strategy_fit`` and reach a real
LLM call (unlike the trigger loop's ``SCANNER_MAX_COUNCIL_RUNS``). Once
the watchlist grew past 100 symbols, a single scheduled sweep made 638
real Anthropic calls in ~90 minutes and drained the account's entire $10
credit balance (confirmed live in Railway logs; see fable5findings.md and
``MIN_FIT_TO_TRADE``'s docstring in strategies/fit.py).

These tests pin the two independent gates ``daily_cron.main`` now applies
to every caller (baseline sweep, trigger loop, CLI — there is exactly one
entry point):

  - ``MAX_LLM_SYMBOLS_PER_SWEEP`` — a ledger-independent hard cap on how
    many symbols may reach a real LLM call in one call to ``main``,
    ranked by strategy-fit score so the best setups get the budget first.
  - ``MAX_DAILY_LLM_SPEND_USD`` — a real-dollar rolling-24h ceiling read
    live off the SAME cost ledger ``/api/v1/health/full`` sums from,
    checked before each admitted candidate actually runs so it can trip
    partway through a sweep that started under budget.

``best_strategy`` is patched on its OWN module (``trading_agents.
strategies``) because ``_score_candidates_for_sweep`` imports it lazily —
patching ``daily_cron.best_strategy`` would miss it, the same convention
``test_daily_cron.py`` already uses for ``is_us_trading_day``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

_USER = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    from trading_agents.cost_ledger import reset_cost_ledger_for_tests
    from trading_agents.memory import reset_memory_stores_for_tests

    reset_memory_stores_for_tests()
    reset_cost_ledger_for_tests()


@pytest.fixture(autouse=True)
def _force_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.features

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: True)


def _patch_scoring(monkeypatch: pytest.MonkeyPatch, scores: dict[str, float | None]) -> None:
    """Fully test-controlled scoring: the fake feature dict carries only a
    ``symbol`` marker, and the fake ``best_strategy`` reads the intended
    score straight off it — independent of ``synthetic_features``' hash-
    seeded behavior, which would make scores unpredictable per symbol."""
    import trading_agents.strategies as strategies_mod
    from trading_agents.jobs import daily_cron

    def _fake_provider(symbol: str, horizon: str = "short"):
        return {"symbol": symbol}

    def _fake_best_strategy(features, *, priors=None, allow_shorts=False):
        score = scores.get(features["symbol"])
        if score is None:
            return None, []
        winner = SimpleNamespace(score=score, strategy_id="fake", direction="long")
        return winner, [winner]

    monkeypatch.setattr(strategies_mod, "best_strategy", _fake_best_strategy)
    monkeypatch.setattr(
        daily_cron, "resolve_feature_provider", lambda **_kw: _fake_provider
    )


async def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    watchlist: list[str],
    scores: dict[str, float | None],
    *,
    on_run_council: Callable[[str], Awaitable[None]] | None = None,
    **env: str | None,
) -> list[str]:
    """Run ``daily_cron.main`` with scoring faked per ``scores`` and
    ``run_council`` replaced by a recorder. Returns the symbols it was
    actually called for, in call order."""
    from trading_agents.jobs import daily_cron

    _patch_scoring(monkeypatch, scores)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    calls: list[str] = []

    async def fake_run_council(**kwargs):
        calls.append(kwargs["symbol"])
        if on_run_council is not None:
            await on_run_council(kwargs["symbol"])
        return {
            "final_action": "HOLD", "selected_strategy": None,
            "selector_confidence": 0.0, "decision_id": f"dec-{kwargs['symbol']}",
        }

    monkeypatch.setattr(daily_cron, "run_council", fake_run_council)
    await daily_cron.main(_USER, watchlist, force=False)
    return calls


# ── Symbol-count cap ─────────────────────────────────────────────────


async def test_symbol_cap_admits_only_the_top_scoring_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scores = {"LOW": 0.50, "HIGH": 0.90, "MID": 0.70}
    calls = await _run_main(
        monkeypatch, ["LOW", "HIGH", "MID"], scores,
        MAX_LLM_SYMBOLS_PER_SWEEP="2",
    )
    assert set(calls) == {"HIGH", "MID"}, (
        "the worst-scoring candidate (LOW) must be the one capped out, "
        f"not whichever came first in watchlist order — got {calls}"
    )


async def test_free_hold_symbols_run_even_when_the_symbol_cap_is_fully_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symbol with no strategy fit at all costs nothing and must never
    be capped out by MAX_LLM_SYMBOLS_PER_SWEEP — only real candidates
    compete for that budget."""
    scores = {"SETUP": 0.90, "NOSETUP": None}
    calls = await _run_main(
        monkeypatch, ["SETUP", "NOSETUP"], scores,
        MAX_LLM_SYMBOLS_PER_SWEEP="1",
    )
    assert set(calls) == {"SETUP", "NOSETUP"}


async def test_watchlist_order_preserved_for_symbols_that_do_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap decides WHICH symbols are admitted, never their run order —
    a regression guard on daily_cron's own "one symbol failing must not
    stop the rest" contract, which is keyed off exact call order
    elsewhere (test_daily_cron.py)."""
    scores = {"GOOD1": 0.80, "BROKE": 0.85, "GOOD2": 0.75}
    calls = await _run_main(
        monkeypatch, ["GOOD1", "BROKE", "GOOD2"], scores,
        MAX_LLM_SYMBOLS_PER_SWEEP="10",
    )
    assert calls == ["GOOD1", "BROKE", "GOOD2"]


# ── Dollar budget ceiling ────────────────────────────────────────────


async def test_symbols_hold_uncosted_when_the_daily_budget_is_already_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_agents.cost_ledger import LedgerEntry, get_cost_ledger

    await get_cost_ledger().record(LedgerEntry(cost_usd=5.0, model="x", role="technical"))

    scores = {"AAA": 0.9, "BBB": 0.8}
    calls = await _run_main(
        monkeypatch, ["AAA", "BBB"], scores,
        MAX_DAILY_LLM_SPEND_USD="3.0",
    )
    assert calls == [], (
        "budget was already over the $3.00 ceiling before the sweep even "
        "started — no admitted candidate should have been allowed to run"
    )


async def test_budget_trips_partway_through_a_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live check runs BEFORE each admitted candidate, not just once
    at the top — a sweep that starts under budget must still stop the
    instant a real call pushes it over, rather than waiting for the next
    sweep to notice."""
    from trading_agents.cost_ledger import LedgerEntry, get_cost_ledger

    ledger = get_cost_ledger()

    async def _spend_on_run(symbol: str) -> None:
        await ledger.record(LedgerEntry(cost_usd=2.0, model="x", role="technical"))

    scores = {"FIRST": 0.9, "SECOND": 0.8, "THIRD": 0.7}
    calls = await _run_main(
        monkeypatch, ["FIRST", "SECOND", "THIRD"], scores,
        on_run_council=_spend_on_run,
        MAX_DAILY_LLM_SPEND_USD="3.0",
    )
    # FIRST runs (spend 0 -> 2). Budget check before SECOND sees $2 < $3,
    # so SECOND runs too (spend 2 -> 4). Budget check before THIRD sees
    # $4 >= $3 -- tripped, THIRD HOLDs uncosted.
    assert calls == ["FIRST", "SECOND"]


# ── Calendar-day window, not rolling 24h ─────────────────────────────


def test_seconds_since_midnight_utc_is_a_calendar_day_not_a_rolling_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the specific fix: a rolling 24h window would keep counting a
    historical spend spike against the budget for a full day after it
    happened, even once the operator has topped up credits — see
    _DEFAULT_MAX_DAILY_LLM_SPEND_USD's docstring. Fixed "now" at 19:30 UTC
    must report 19h30m elapsed since midnight, not a constant 24h."""
    import datetime as real_datetime

    from trading_agents.jobs import daily_cron as cron

    fixed_now = real_datetime.datetime(2026, 9, 1, 19, 30, 0, tzinfo=real_datetime.UTC)

    class _FixedDatetime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(cron, "datetime", _FixedDatetime)

    assert cron._seconds_since_midnight_utc() == real_datetime.timedelta(
        hours=19, minutes=30
    )


# ── Env var readers — malformed input keeps the default ─────────────


def test_max_daily_llm_spend_usd_defaults_and_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_agents.jobs import daily_cron as cron

    monkeypatch.delenv("MAX_DAILY_LLM_SPEND_USD", raising=False)
    assert cron._max_daily_llm_spend_usd() == pytest.approx(3.0)

    monkeypatch.setenv("MAX_DAILY_LLM_SPEND_USD", "1.5")
    assert cron._max_daily_llm_spend_usd() == pytest.approx(1.5)

    monkeypatch.setenv("MAX_DAILY_LLM_SPEND_USD", "not-a-number")
    assert cron._max_daily_llm_spend_usd() == pytest.approx(3.0)

    monkeypatch.setenv("MAX_DAILY_LLM_SPEND_USD", "-5")
    assert cron._max_daily_llm_spend_usd() == pytest.approx(3.0)


def test_max_llm_symbols_per_sweep_defaults_and_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_agents.jobs import daily_cron as cron

    monkeypatch.delenv("MAX_LLM_SYMBOLS_PER_SWEEP", raising=False)
    assert cron._max_llm_symbols_per_sweep() == 15

    monkeypatch.setenv("MAX_LLM_SYMBOLS_PER_SWEEP", "5")
    assert cron._max_llm_symbols_per_sweep() == 5

    monkeypatch.setenv("MAX_LLM_SYMBOLS_PER_SWEEP", "not-a-number")
    assert cron._max_llm_symbols_per_sweep() == 15

    monkeypatch.setenv("MAX_LLM_SYMBOLS_PER_SWEEP", "0")
    assert cron._max_llm_symbols_per_sweep() == 15
