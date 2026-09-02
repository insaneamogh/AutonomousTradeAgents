"""Eval assertions over the 100-case golden dataset.

Runs in CI: no network, no credentials, no LLM calls, under a second.

Two kinds of test live here and they are labelled, because they fail for
different reasons and want different responses:

  * **Contract tests** assert something that must always be true (a
    refusal carries a named reason; the funnel is deterministic; the
    thin-evidence gate refuses). A failure is a bug.
  * **Characterisation tests** record a MEASURED property of the current
    system (how much of the dataset reaches an LLM; how much dynamic
    range the strategy score actually has). A failure means the system's
    behaviour moved — which may be an improvement. Read the number, then
    decide.

Mixing the two silently is how a suite ends up pinning an accident as if
it were a requirement.
"""

from __future__ import annotations

import pytest
from tests.eval.funnel import run_all, run_funnel
from tests.eval.scenarios import golden_scenarios, scenarios_by_archetype


@pytest.fixture(scope="module")
def report():
    return run_all(golden_scenarios())


# ── Dataset integrity ────────────────────────────────────────────────


def test_the_golden_dataset_is_one_hundred_cases() -> None:
    scenarios = golden_scenarios()
    assert len(scenarios) == 100
    assert len({s.case_id for s in scenarios}) == 100, "case ids must be unique"
    assert len(scenarios_by_archetype()) == 10


def test_every_scenario_carries_a_human_readable_justification() -> None:
    """A labelled dataset whose labels have no stated reasoning is not a
    golden dataset, it is 100 hardcoded outputs. When a case fails, `why`
    is what tells you whether the code or the label is wrong."""
    for s in golden_scenarios():
        assert s.why.strip(), s.case_id
        assert len(s.why) > 40, f"{s.case_id}: justification too thin to be useful"


# ── Contract: the deterministic layer actually fires ─────────────────


def test_the_funnel_refuses_a_meaningful_share_before_any_llm_call(report) -> None:
    """The headline question: does pure maths actually filter?

    A funnel that passes everything is not a funnel — it is a pass-through
    that costs money. A funnel that passes nothing never trades. Both are
    failures and this asserts we are in neither.
    """
    assert report.refused > 0, (
        "the deterministic layer refused NOTHING — every symbol would reach "
        "a paid LLM pass, which is the exact cost failure the screen exists "
        "to prevent"
    )
    assert report.reached_llm > 0, (
        "the deterministic layer refused EVERYTHING — the desk would never "
        "trade, which is worse than spending on a mediocre setup"
    )


def test_every_refusal_carries_a_named_reason(report) -> None:
    """The Refusal Ledger is this project's whole differentiator. An
    unnamed refusal is a defect even when the refusal itself is correct —
    it cannot be counted, priced, or shown to a judge."""
    unnamed = [r.case_id for r in report.results
               if not r.reaches_llm and not r.refusal_reason]
    assert not unnamed, f"refusals with no named reason: {unnamed}"


def test_the_funnel_is_deterministic(report) -> None:
    """Same inputs, same verdicts — twice. Non-determinism here would make
    every other assertion in this file meaningless, and would mean two
    scans of an unchanged market could disagree."""
    again = run_all(golden_scenarios())
    assert [r.refusal_reason for r in report.results] == \
           [r.refusal_reason for r in again.results]
    assert [r.qty for r in report.results] == [r.qty for r in again.results]
    assert [r.score for r in report.results] == [r.score for r in again.results]


# ── Contract: per-archetype behaviour ────────────────────────────────


def test_no_edge_scenarios_never_reach_an_llm() -> None:
    """Flat, directionless names are most of any watchlist on most days.
    Every one that reaches a model is money spent on a foregone
    conclusion."""
    for s in scenarios_by_archetype()["choppy_nothing"]:
        result = run_funnel(s)
        assert not result.reaches_llm, f"{s.case_id} reached an LLM: {s.why}"


def test_thin_evidence_is_refused_not_scored_as_a_good_setup() -> None:
    """Regression guard on the empty-dict bug: `best_strategy({})` used to
    return rsi_mean_reversion at 0.60 because the 'unknown' trend sentinel
    satisfied `not_a_trend_break` as a genuine TRUE rather than NEUTRAL.
    Absence of evidence is not evidence of a good setup."""
    for s in scenarios_by_archetype()["thin_evidence"]:
        result = run_funnel(s)
        assert not result.reaches_llm, f"{s.case_id} reached an LLM: {s.why}"
        assert result.refusal_reason == "below_fit_floor_or_thin_evidence"


def test_a_shallow_chain_is_refused_before_the_debate_is_paid_for() -> None:
    """The CME shape. The UNDERLYING looks good — that is the trap — so the
    refusal must come from the chain, and it must come before any model
    call. CME261016P00270000 cost ~3 Sonnet calls to discover this."""
    for s in scenarios_by_archetype()["illiquid_chain"]:
        result = run_funnel(s)
        assert not result.reaches_llm, f"{s.case_id} reached an LLM"
        assert result.refusal_reason in ("illiquid_chain", "no_liquid_contract")


def test_a_thin_contract_is_trimmed_not_vetoed() -> None:
    """Sizing TRIMS; refusals belong upstream where they get a named
    ledger reason. A thin contract must still trade — smaller."""
    for s in scenarios_by_archetype()["thin_open_interest"]:
        result = run_funnel(s)
        assert result.reaches_llm, f"{s.case_id} was vetoed; it should be trimmed"
        assert result.qty is not None and result.qty >= 1
        cap = s.options["expect_qty_at_most"]  # type: ignore[index]
        assert result.qty <= cap, (
            f"{s.case_id}: {result.qty} contracts exceeds 1% of "
            f"{s.options['open_interest']} open interest"  # type: ignore[index]
        )


def test_the_liquidity_trim_does_not_bind_on_liquid_contracts() -> None:
    """The cap's value is its ASYMMETRY: it shrinks doubtful positions and
    leaves everything else alone. A trim that binds everywhere is just a
    smaller book with extra steps."""
    for s in scenarios_by_archetype()["liquid_options"]:
        result = run_funnel(s)
        assert result.reaches_llm
        assert result.sizing_note and "liquidity cap" not in result.sizing_note, (
            f"{s.case_id}: the trim bound on a contract with "
            f"{s.options['open_interest']} open interest"  # type: ignore[index]
        )


def test_every_option_that_reaches_the_llm_has_a_placeable_stop(report) -> None:
    """A position we would open must have a resting protective level we
    could actually place. A stop that rounds into unfillable dust is worse
    than none — the audit row claims protection that does not exist."""
    for r in report.results:
        if r.reaches_llm and r.qty is not None:
            assert r.stop_price and r.stop_price > 0, r.case_id
            assert r.limit_price and 0 < r.limit_price < r.stop_price, r.case_id


# ── Characterisation: MEASURED, not required ─────────────────────────


def test_measured_llm_admission_rate(report) -> None:
    """CHARACTERISATION. Records what share of the dataset reaches a paid
    pass today. Not a requirement — a tripwire.

    Measured 2026-09-02: 60/100. That is loose for the intended shape
    (scan wide, debate a handful), and the reason is recorded in
    ``test_measured_strategy_score_dynamic_range`` below: the score has
    almost no spread, so there is no "top decile" to select. Widen the
    band here only with a reason.
    """
    assert 0.20 <= report.llm_fraction <= 0.75, (
        f"admission rate moved to {report.llm_fraction:.0%} (was 60% on "
        "2026-09-02). Not necessarily wrong — but find out why before "
        "widening this band."
    )


def test_measured_strategy_score_dynamic_range() -> None:
    """CHARACTERISATION, and the most important number this suite produces.

    Measured 2026-09-02 across 300 symbols from the repo's own synthetic
    feature generator: every symbol that clears the fit floor scores
    between **0.6075 and 0.6107** — 18 distinct values inside a 0.3%
    band. On the hand-built archetypes in this dataset the opposite
    happens: 54 of 60 passers saturate at exactly 1.000.

    Two consequences, both real:

      1. **A score floor cannot work as a quality dial.** Any threshold
         either admits everything or nothing — there is a cliff between
         0.60 and 0.65 with nothing inside it. ``MIN_LLM_SCORE`` is
         therefore not the tuning knob it looks like.
      2. **Ranking by score is near-arbitrary here.** When candidates tie
         to three decimal places, ``sorted(key=-score)`` is decided by the
         tie-break, not by quality. Ordering the paid loop by score is
         still strictly better than walking watchlist order — it is
         deterministic and independent of list position — but it does NOT
         deliver "the caps ration to the best setups" on data that looks
         like this.

    UNVERIFIED and important: this is measured on SYNTHETIC features,
    which derive from one hash seed per symbol and are low-variance by
    construction. Whether real Alpaca features spread the score out is
    unmeasured — it needs live keys. That measurement is the first thing
    to run against the live feature provider.
    """
    from trading_agents.features.synthetic import synthetic_features
    from trading_agents.strategies import best_strategy

    scores = []
    for i in range(300):
        winner, _ = best_strategy(synthetic_features(f"SYM{i:03d}"), allow_shorts=False)
        if winner is not None:
            scores.append(winner.score)

    assert scores, "no symbol cleared the fit floor — the screen is refusing everything"
    spread = max(scores) - min(scores)
    assert spread < 0.10, (
        f"score spread is now {spread:.4f} (was 0.0032 on 2026-09-02). If this "
        "grew, the score became a usable ranking signal and MIN_LLM_SCORE / "
        "best-of-window ranking are now worth revisiting — update this test "
        "and say so."
    )
