"""Council tests.

Two flavors:
  - **Mock-LLM** (default): exercises the council under the deterministic
    mock responses. Runs in CI, no API key needed.
  - **Real-LLM** (opt-in via ``RUN_REAL_LLM_TESTS=1``): hits real Anthropic.
    Costs real money. Skipped automatically when the env var is unset OR
    ``ANTHROPIC_API_KEY`` isn't available.
"""

from __future__ import annotations

import os

import pytest

from trading_agents.llm import LLM
from trading_agents.runtime import run_council

# ─────────────────────────────────────────────────────────────────────
# Mock-LLM tests — always run
# ─────────────────────────────────────────────────────────────────────


async def test_mock_council_produces_buy_proposal_for_nvda() -> None:
    llm = LLM(api_key=None)  # force mock mode
    assert llm.mock is True

    result = await run_council(symbol="NVDA", llm=llm)
    assert result["llm_mock"] is True
    assert result["final_action"] in ("BUY", "SELL", "HOLD", "VETOED")
    # Three analysts ran (router includes them all in mock mode).
    assert result["technical"] is not None
    assert result["fundamental"] is not None
    assert result["macro"] is not None
    # Macro analyst gives a real score (not the parse-error default).
    assert result["macro"]["score"] == pytest.approx(60.0)
    assert result["macro"]["confidence"] == pytest.approx(0.50)


async def test_mock_council_proposal_carries_sizing_metadata() -> None:
    llm = LLM(api_key=None)
    result = await run_council(symbol="AAPL", llm=llm)
    if result["proposal"] is not None:
        assert result["proposal"]["stopLoss"] is not None
        assert result["proposal"]["targetPrice"] is not None
        assert isinstance(result["proposal"]["informationalFlags"], list)


async def test_strategy_fit_and_drafter_both_fire() -> None:
    """The deterministic fit node picks a strategy; the Drafter turns it
    into a proposal.

    The pick is no longer an LLM's: ``selected_strategy`` comes from the
    precondition scorers and ``selector_rationale`` is a NAMED reason of
    the form ``<strategy>_<direction>:<component>+<component>``. Both
    surfaces land on the runtime result — they are the contract the
    Reflection Agent scores later.
    """
    llm = LLM(api_key=None)
    result = await run_council(symbol="NVDA", llm=llm)

    from trading_agents.strategies import STRATEGY_REGISTRY

    assert result["selected_strategy"] in STRATEGY_REGISTRY
    assert result["selected_direction"] in ("long", "short")
    assert 0.0 < result["selector_confidence"] <= 1.0
    # The named-reason shape, not model prose.
    assert result["selector_rationale"].startswith(result["selected_strategy"])
    assert result["selected_direction"] in result["selector_rationale"]

    # The full fit block is carried for the audit row + thesis view.
    fit = result["strategy_fit"]
    assert fit["winner"]["strategy_id"] == result["selected_strategy"]
    assert fit["winner"]["components"], "components explain WHY it fit"
    assert fit["ranked"], "the alternatives that lost are kept too"

    if result["proposal"] is not None:
        assert result["proposal"]["side"] in ("BUY", "SELL")
        assert result["proposal"]["bullCase"]
        assert result["proposal"]["bearCase"]


async def test_no_strategy_fit_holds_without_spending_an_llm_call() -> None:
    """The cost win, pinned: a symbol whose setup fits nothing must HOLD
    with ZERO calls to the LLM — not five.

    We count every ``complete`` invocation rather than asserting on the
    result alone, because "returns HOLD" was already true before; what is
    new is that it costs nothing.
    """
    calls: list[str] = []

    class _CountingLLM:
        mock = True

        async def complete(self, *, system, user, **_kw):
            calls.append(system[:40])
            raise AssertionError("no LLM call may happen on a no-fit symbol")

    def _featureless(symbol: str, horizon: str = "short") -> dict:
        # Every precondition maximally unsatisfied: no trend, mid-channel,
        # no momentum, exploded vol, perfectly correlated to the index.
        return {
            "symbol": symbol,
            "horizon": horizon,
            "last_price": 100.0,
            "portfolio_equity": 100_000.0,
            "technicals": {
                "trend_regime": "choppy",
                "dma20_pct": -0.05,
                "dma50_pct": -0.05,
                "rsi_14": 50.0,
                "atr_14": 2.0,
                "volume_ratio_20d": 0.5,
            },
            "quant": {
                "ret_252d_pct": -0.2,
                "ret_63d_pct": -0.3,
                "ret_21d_pct": -0.4,
                "sharpe": -0.4,
                "atr_zscore": 3.0,
                "realized_vol_pct": 85.0,
                "corr_benchmark": 0.99,
                "price_zscore_20": 0.0,
                "donchian_pct": 50.0,
            },
        }

    result = await run_council(
        symbol="NOFIT", llm=_CountingLLM(), feature_provider=_featureless
    )

    assert calls == [], f"expected zero LLM calls, got {calls}"
    assert result["final_action"] == "HOLD"
    assert result["selected_strategy"] is None
    assert result["proposal"] is None
    assert "clears the fit floor" in result["selector_rationale"]


async def test_fit_only_ever_returns_registry_ids() -> None:
    """A whole class of bug is gone: the old LLM Selector could hallucinate
    a strategy id and needed a fallback path. The scorers are keyed off the
    registry itself, so an unknown id is unrepresentable.
    """
    from trading_agents.strategies import STRATEGY_REGISTRY, rank_strategies
    from trading_agents.strategies.fit import _SCORERS

    assert set(_SCORERS) == set(STRATEGY_REGISTRY)
    ranked = rank_strategies({"technicals": {"trend_regime": "uptrend"}}, allow_shorts=True)
    assert {r.strategy_id for r in ranked} <= set(STRATEGY_REGISTRY)


async def test_run_council_attributes_every_llm_call_to_its_run_and_user() -> None:
    """Regression test for the cost-attribution gap this fix closes: before
    it, ``llm_calls.agent_decision_id`` and ``llm_calls.user_id`` were
    unconditionally NULL on every row — no way to answer "which LLM calls
    produced decision X" or "how much did user Y's trading cost in LLM
    spend". Exercised end-to-end against the mock LLM (no Postgres needed):
    ``InMemoryCostLedger`` + ``InMemoryDecisionLog`` implement the same
    Protocols the Postgres-backed classes do, so the wiring under test —
    ``runtime.run_council`` threading ``council_run_id``/``user_id`` through
    every node's ``complete_json`` call, then backfilling the real decision
    id once it exists — is identical either way.

    This would NOT have passed before this change: ``council_run_id`` didn't
    exist as a concept anywhere (state key, LedgerEntry field, or Protocol
    method), every row's ``user_id`` was unconditionally None because
    ``_record_to_ledger`` never received or forwarded it, and nothing ever
    called back into the ledger after ``decision_log.record()`` to attach
    ``agent_decision_id`` — so (a), (b), and (c) below each pin a hard
    failure against the pre-change code, not just a shape check.
    """
    from trading_agents.cost_ledger import get_cost_ledger, reset_cost_ledger_for_tests
    from trading_agents.memory import InMemoryDecisionLog

    reset_cost_ledger_for_tests()  # this file has no autouse reset fixture
    llm = LLM(api_key=None)
    decision_log = InMemoryDecisionLog()

    result = await run_council(
        symbol="NVDA", llm=llm, decision_log=decision_log, user_id="user-42"
    )

    rows = await get_cost_ledger().all()
    # NVDA clears the fit floor under the mock provider (see
    # test_mock_council_produces_buy_proposal_for_nvda above) and runs the
    # full council, so this pass must have written at least one row.
    assert rows, "expected at least one llm_calls row for this pass"

    # (a) every LLM call row from this run shares ONE council_run_id.
    run_ids = {r.council_run_id for r in rows}
    assert run_ids == {r.council_run_id for r in rows if r.council_run_id is not None}, (
        "every row must carry a council_run_id — none may be None"
    )
    assert len(run_ids) == 1

    # (b) that value equals the returned decision's id — proving the
    # post-hoc backfill in runtime.run_council actually ran and matched.
    (council_run_id,) = run_ids
    assert result["decision_id"] is not None
    assert council_run_id == result["decision_id"]
    assert all(r.agent_decision_id == result["decision_id"] for r in rows)

    # (c) user_id populated on every row when run_council was called with one.
    assert all(r.user_id == "user-42" for r in rows)


async def test_run_council_instrument_preference_reaches_strategy_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_council()``'s ``instrument_preference`` kwarg must actually
    reach ``strategy_fit_node``'s gate — proving the full plumbing (API
    request shape -> ``run_council`` -> ``CouncilState`` ->
    ``strategy_fit_node``) works end-to-end, not just that a hand-built
    ``CouncilState`` can exercise the branch in isolation (which is all
    the options-drafter unit tests prove on their own).

    Structural proof, not an inference from a HOLD-reason string (the
    top-level result dict's ``risk_reason`` is the same generic
    "No proposal — HOLD." for every proposal-less HOLD regardless of WHY,
    so it can't distinguish the two paths on its own): patch
    ``drafter._fetch_option_candidates`` with a call-counting spy and
    assert it was actually invoked. No Alpaca keys are configured in this
    test environment, so the real chain fetch would find no candidates
    and the run HOLDs either way — the point here is proving the run
    entered the options branch AT ALL, not exercising contract selection.
    """
    from trading_agents.nodes import drafter as drafter_mod

    calls: list[str] = []

    async def _spy_fetch(symbol: str) -> tuple[object, ...]:
        calls.append(symbol)
        return ()

    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.setattr(drafter_mod, "_fetch_option_candidates", _spy_fetch)
    llm = LLM(api_key=None)

    result = await run_council(symbol="NVDA", llm=llm, instrument_preference="option")

    assert calls == ["NVDA"], (
        "expected the options branch's chain fetch to run exactly once — "
        f"instrument_preference did not reach strategy_fit_node/drafter_node "
        f"(calls={calls})"
    )
    assert result["final_action"] == "HOLD"
    assert result["proposal"] is None


async def test_run_council_instrument_preference_ignored_without_allow_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both ALLOW_OPTIONS and instrument_preference are required — the env
    flag alone gates whether a preference does anything at all.

    Same structural-spy approach as the test above, not an assertion on
    ``final_action`` — synthetic features are hash-seeded per (symbol,
    day) (see ``test_mock_council_produces_buy_proposal_for_nvda``'s own
    deliberately loose assertion for the same reason), so NVDA's exact
    mock-LLM outcome can legitimately differ by the calendar date this
    runs on. What must NOT vary by date is whether the options branch
    was entered at all.
    """
    from trading_agents.nodes import drafter as drafter_mod

    calls: list[str] = []

    async def _spy_fetch(symbol: str) -> tuple[object, ...]:
        calls.append(symbol)
        return ()

    monkeypatch.delenv("ALLOW_OPTIONS", raising=False)
    monkeypatch.setattr(drafter_mod, "_fetch_option_candidates", _spy_fetch)
    llm = LLM(api_key=None)

    result = await run_council(symbol="NVDA", llm=llm, instrument_preference="option")

    assert calls == [], "options chain fetch must not run without ALLOW_OPTIONS"
    assert result["final_action"] in ("BUY", "SELL", "HOLD", "VETOED")
    if result["proposal"] is not None:
        assert result["proposal"].get("isOption") in (None, False)


async def test_run_council_options_proposal_reaches_evaluate_option_and_is_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capstone cross-track proof: a full options proposal, built by
    the real drafter_node against a synthetic-but-liquid contract, must
    flow all the way through risk_officer_node -> engine.risk.engine
    .evaluate() -> the is_option dispatch -> evaluate_option() and come
    back APPROVED.

    This is the one thing neither options track could prove on its own —
    each tested its own half in isolation (the drafter track against a
    hand-built RiskProposal spy; the risk-rules track against a
    hand-built OptionLegDetails). It was structurally impossible to prove
    end-to-end until both tracks AND the risk_officer_node/
    options_trading_level glue fixes existed together. If any of those
    seams is wrong — a field name mismatch, risk_officer_node not
    threading is_option, options_trading_level still None everywhere —
    this test fails; if they all agree, it doesn't.
    """
    from datetime import UTC, datetime, timedelta

    from engine.options.selection import ContractQuote
    from trading_agents.nodes import drafter as drafter_mod

    # Matches the options-drafter unit tests' own fixture exactly (a
    # contract already proven, in isolation, to clear every selection
    # stage) — the point here is the INTEGRATION, not re-deriving a new
    # synthetic contract from scratch.
    quote = ContractQuote(
        occ_symbol="NVDA_TEST_CALL",
        contract_type="call",
        strike=250.0,
        expiry=(datetime.now(UTC) + timedelta(days=30)).date(),
        bid=3.00,
        ask=3.20,
        open_interest=500,
        volume=200,
        delta=0.55,
        implied_volatility=0.30,
    )

    # Chain DEPTH: `select_contract` refuses a chain yielding fewer than
    # `_MIN_LIQUID_CHAIN_DEPTH` liquidity survivors (see its docstring —
    # the 2026-09-01 CME position, 1 of 29, whose mark then gapped 26
    # points in one print so the stop could not function). Siblings are
    # wider-spread (3.05/3.35 vs 3.00/3.20) so tie-break still returns the
    # contract this test names, and both stay under the 12% spread cap.
    _siblings = tuple(
        ContractQuote(
            occ_symbol=f"NVDA_TEST_CALL_SIB{i}",
            contract_type="call",
            strike=250.0 + i,
            expiry=(datetime.now(UTC) + timedelta(days=30)).date(),
            bid=3.05,
            ask=3.35,
            open_interest=500,
            volume=200,
            delta=0.55,
            implied_volatility=0.30,
        )
        for i in range(1, 6)
    )

    async def _fake_fetch(symbol: str) -> tuple[ContractQuote, ...]:
        return (quote, *_siblings)

    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.setattr(drafter_mod, "_fetch_option_candidates", _fake_fetch)
    llm = LLM(api_key=None)

    result = await run_council(symbol="NVDA", llm=llm, instrument_preference="option")

    # NO HOLD escape hatch. `features.synthetic._hash_seed` keys on the
    # SYMBOL ONLY, so "NVDA" deterministically produces the same feature
    # dict on every run — it yields momentum long at fit ~0.90, far above
    # MIN_FIT_TO_TRADE. (Was "sma_crossover at 0.787" before
    # docs/PLAN_AGGRESSIVE_PROFILE.md's `best_strategy` evidence gate made
    # synthetic_features grow a "quant" block to stop looking like a data
    # outage — the exact winner shifted, the "always clears the floor"
    # invariant this comment documents did not.) The "the fit node can
    # legitimately HOLD" justification this test used to carry was simply
    # false for this fixture, and the early `return` it guarded meant the
    # approval assertions below NEVER EXECUTED. That is what hid a live
    # bug where risk_officer passed the underlying's share price as the
    # per-contract premium, so `max_premium_pct` vetoed 100% of real
    # options proposals ("68.71% of equity, cap 1.00%") while this test
    # stayed green.
    assert result["final_action"] == "BUY"
    proposal = result["proposal"]
    assert proposal is not None
    assert proposal["isOption"] is True
    assert proposal["occSymbol"] == "NVDA_TEST_CALL"
    # `symbol` stays the UNDERLYING; only the wire carries the contract.
    assert proposal["symbol"] == "NVDA"
    # The arithmetic that the premium-units bug got wrong: qty * ask *
    # multiplier, NOT qty * underlying_price * multiplier. Without this
    # line a regression to the underlying still passes as long as the
    # resulting trim happens to round to >= 1 contract.
    assert proposal["estimatedNotional"] == pytest.approx(
        proposal["qty"] * 3.20 * 100, abs=0.01
    )
    assert result["risk_approved"] is True

    # The actual capstone assertions: it was RISK-EVALUATED (not skipped),
    # and it came back approved via the options pipeline specifically.
    assert result["risk_approved"] is True
    assert result["risk_veto_rule"] is None
    assert "options_disabled" not in (result.get("risk_reason") or "")


async def test_analysts_run_concurrently() -> None:
    """Technical/Fundamental/Macro must run as ONE concurrent hop, not
    three sequential ones.

    Mirrors ``test_agents_run_concurrently`` in ``test_options_agents.py``
    exactly, and for the same reason: a sequential ``await tech();
    await fund(); await macro()`` still satisfies a call-count assertion
    while silently tripling wall-clock latency for any pass that runs all
    three analysts — which, before this test existed, is exactly what
    ``graph.py`` did (a plain ``for`` loop over the three node functions
    in ``_run_linear``, and a technical -> fundamental -> macro conditional
    chain of separate LangGraph nodes in ``_build_langgraph``). Asserting
    on elapsed time is the only way to pin "actually concurrent" rather
    than "eventually all awaited".
    """
    import asyncio
    import json
    import time
    from typing import Any

    from trading_agents.graph import _run_analysts_parallel
    from trading_agents.llm import LLMResponse

    delay = 0.2
    body = json.dumps({"score": 60.0, "confidence": 0.5, "thesis": "t", "citations": []})

    class _SlowLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, *, system: str, user: str, **kwargs: Any) -> LLMResponse:
            self.calls += 1
            await asyncio.sleep(delay)
            return LLMResponse(text=body, model="test")

    fake = _SlowLLM()
    state = {"symbol": "NVDA", "horizon": "short", "context": {}}
    start = time.monotonic()
    result = await _run_analysts_parallel(state, fake, ["technical", "fundamental", "macro"])
    elapsed = time.monotonic() - start

    assert fake.calls == 3
    assert result["technical"]["score"] == pytest.approx(60.0)
    assert result["fundamental"]["score"] == pytest.approx(60.0)
    assert result["macro"]["score"] == pytest.approx(60.0)
    # Sequential execution would take ~3x delay; concurrent stays near 1x.
    assert elapsed < delay * 1.6, (
        f"expected concurrent execution, took {elapsed:.3f}s for 3x{delay}s calls"
    )


async def test_analysts_parallel_merges_degraded_nodes_without_duplication() -> None:
    """``_merge_analyst_results`` reconstructs the shared ``degraded_nodes``
    list by hand (see ``graph.py``'s module docstring for why: LangGraph
    forbids >1 writer to the same channel key per step, which is exactly
    what three concurrently-run analysts would otherwise be). Pin that the
    merge names EXACTLY the analysts whose OWN call degraded — not zero,
    not all three, and not duplicated when more than one degrades.
    """
    from trading_agents.graph import _run_analysts_parallel
    from trading_agents.llm import LLMResponse

    class _SelectivelyBrokenLLM:
        """Fundamental and macro return unparseable JSON; technical is fine."""

        async def complete(self, *, system: str, user: str, **kwargs) -> LLMResponse:  # type: ignore[no-untyped-def]
            role_line = system[:120].lower()
            if "fundamental" in role_line or "macro" in role_line:
                return LLMResponse(text="not json", model="test")
            return LLMResponse(
                text='{"score": 70.0, "confidence": 0.6, "thesis": "ok", "citations": []}',
                model="test",
            )

    state = {"symbol": "NVDA", "horizon": "short", "context": {}}
    result = await _run_analysts_parallel(
        state, _SelectivelyBrokenLLM(), ["technical", "fundamental", "macro"]
    )

    assert sorted(result["degraded_nodes"]) == ["fundamental", "macro"]
    assert result["technical"]["score"] == pytest.approx(70.0)
    # Degraded analysts still produce the neutral fallback, not a crash.
    assert result["fundamental"]["score"] == pytest.approx(50.0)
    assert result["macro"]["score"] == pytest.approx(50.0)


async def test_drafter_skipped_when_fit_holds() -> None:
    """Integration: a HOLD from the deterministic fit node must skip the
    Router, every analyst, and the Drafter — the whole rest of the graph.
    """
    from trading_agents import graph as graph_mod

    async def _hold_fit(state):
        return {
            **state,
            "selected_strategy": None,
            "selected_direction": None,
            "selector_confidence": 0.0,
            "selector_rationale": "STUB-HOLD: forced HOLD from test.",
            "proposal": None,
            "final_action": "HOLD",
        }

    original = graph_mod.strategy_fit_node
    graph_mod.strategy_fit_node = _hold_fit  # type: ignore[assignment]
    try:
        llm = LLM(api_key=None)
        result = await run_council(symbol="META", llm=llm)
    finally:
        graph_mod.strategy_fit_node = original  # type: ignore[assignment]

    assert result["selected_strategy"] is None
    assert result["proposal"] is None
    assert result["final_action"] == "HOLD"
    assert "STUB-HOLD" in result["selector_rationale"]
    # The Router never ran, so there is no regime.
    assert result["regime"] is None


# ─────────────────────────────────────────────────────────────────────
# Real-LLM smoke — opt-in, costs money
# ─────────────────────────────────────────────────────────────────────


def _real_llm_available() -> bool:
    if os.environ.get("RUN_REAL_LLM_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        return False
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark_real_llm = pytest.mark.skipif(
    not _real_llm_available(),
    reason="Real-LLM tests opt-in via RUN_REAL_LLM_TESTS=1 + ANTHROPIC_API_KEY set.",
)


@pytestmark_real_llm
async def test_real_anthropic_council_produces_proposal() -> None:
    """Hits real Anthropic. Costs ~$0.001 with Haiku for the analyst calls."""
    llm = LLM()  # picks up ANTHROPIC_API_KEY
    assert llm.mock is False

    result = await run_council(symbol="NVDA", llm=llm)
    assert result["final_action"] in ("BUY", "SELL", "HOLD", "VETOED")
    # No MOCK markers in the analyst output.
    for key in ("technical", "fundamental", "macro"):
        if result.get(key):
            assert "MOCK" not in result[key].get("thesis", ""), (
                f"{key} thesis still contains MOCK marker — wrapper picked mock path"
            )


async def test_options_pass_persists_the_contract_funnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`select_contract` is where MOST refusals happen, and every one of
    them used to be logger.info'd and dropped: a HOLD writes no proposal,
    so there was nowhere to put it. The funnel now rides in the decision's
    `reasoning` JSONB on BOTH the approved and the refused path.
    """
    from datetime import UTC, datetime, timedelta

    from engine.options.selection import ContractQuote
    from trading_agents.nodes import drafter as drafter_mod

    quote = ContractQuote(
        occ_symbol="NVDA_TEST_CALL",
        contract_type="call",
        strike=250.0,
        expiry=(datetime.now(UTC) + timedelta(days=30)).date(),
        bid=3.00,
        ask=3.20,
        open_interest=500,
        volume=200,
        delta=0.55,
        implied_volatility=0.30,
    )

    # Chain DEPTH — see `_MIN_LIQUID_CHAIN_DEPTH`. Siblings are
    # wider-spread (3.05/3.35 vs 3.00/3.20) so tie-break still selects the
    # contract named below; both stay under the 12% spread cap.
    _siblings = tuple(
        ContractQuote(
            occ_symbol=f"NVDA_TEST_CALL_SIB{i}",
            contract_type="call",
            strike=250.0 + i,
            expiry=(datetime.now(UTC) + timedelta(days=30)).date(),
            bid=3.05,
            ask=3.35,
            open_interest=500,
            volume=200,
            delta=0.55,
            implied_volatility=0.30,
        )
        for i in range(1, 6)
    )

    async def _fake_fetch(symbol: str) -> tuple[ContractQuote, ...]:
        return (quote, *_siblings)

    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.setattr(drafter_mod, "_fetch_option_candidates", _fake_fetch)

    result = await run_council(
        symbol="NVDA", llm=LLM(api_key=None), instrument_preference="option"
    )

    funnel = result["reasoning"]["contract_funnel"]
    assert funnel is not None
    assert funnel["selected_occ"] == "NVDA_TEST_CALL"
    assert funnel["rejection_reason"] is None
    # Real per-stage counts, not an empty dict — the narrowing IS the story.
    assert funnel["counts"]
    assert all(isinstance(v, int) for v in funnel["counts"].values())


async def test_contract_funnel_explains_an_options_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the user actually hit: "it just says HOLD, no explanation."
    An empty chain must produce a named rejection reason on the row."""
    from trading_agents.nodes import drafter as drafter_mod

    async def _empty_chain(symbol: str) -> tuple:
        return ()

    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.setattr(drafter_mod, "_fetch_option_candidates", _empty_chain)

    result = await run_council(
        symbol="NVDA", llm=LLM(api_key=None), instrument_preference="option"
    )

    assert result["final_action"] == "HOLD"
    funnel = result["reasoning"]["contract_funnel"]
    assert funnel is not None
    assert funnel["rejection_reason"] == "no_candidates"
    assert funnel["selected_occ"] is None


async def test_equity_pass_has_no_contract_funnel() -> None:
    """No option chain was consulted, so claiming a funnel would be
    fabricating a stage that never ran."""
    result = await run_council(symbol="NVDA", llm=LLM(api_key=None))
    assert result["reasoning"]["contract_funnel"] is None
