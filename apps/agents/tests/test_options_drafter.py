"""Options Phase A — strategy_fit's instrument gate + drafter's contract
selection/sizing branch, under the mock LLM.

Mirrors ``test_drafter_hold_reasoning.py``'s style: direct node calls with
a hand-built state + a monkeypatched ``complete_json``/chain-fetch, rather
than the full graph — deterministic, no network, no real LLM, and it
exercises exactly the code this task added rather than depending on the
mock LLM's un-pinned "natural" verdict for some real symbol.

Also carries the proactive cross-boundary regression test: an options
proposal must never accidentally satisfy ``short_requires_stop`` (see that
rule's own docstring, and the 5 commits the short-position work spent
chasing exactly this class of bug — a field one rule needs silently
missing from what another code path produces).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from engine.options.selection import ContractQuote
from engine.risk import RiskCaps, RiskContext, RiskProposal, Side, opens_short
from engine.risk.rules.short_requires_stop import short_requires_stop
from trading_agents.nodes import drafter as drafter_mod
from trading_agents.nodes import strategy_fit as strategy_fit_mod

_NOW = datetime.now(UTC)


def _quote(
    *,
    contract_type: str = "call",
    occ_symbol: str = "AAPL_TEST_CALL",
    strike: float = 250.0,
    dte: int = 30,
    bid: float = 3.00,
    ask: float = 3.20,
    open_interest: int = 500,
    volume: int = 200,
    delta: float = 0.55,
    implied_volatility: float | None = 0.30,
) -> ContractQuote:
    return ContractQuote(
        occ_symbol=occ_symbol,
        contract_type=contract_type,  # type: ignore[arg-type]
        strike=strike,
        expiry=(_NOW + timedelta(days=dte)).date(),
        bid=bid,
        ask=ask,
        open_interest=open_interest,
        volume=volume,
        delta=delta,
        implied_volatility=implied_volatility,
    )


def _drafter_state(**overrides: object) -> dict:
    base = {
        "symbol": "AAPL",
        "selected_strategy": "momentum",
        "selected_direction": "long",
        "selector_confidence": 0.7,
        "selector_rationale": "momentum_long:trailing_3m_return",
        "horizon": "short",
        "regime": "choppy",
        "instrument": "option",
        "context": {
            "last_price": 250.0,
            "portfolio_equity": 100_000.0,
            "technicals": {},
            "options_context": {"days_to_earnings": 12},
        },
        "technical": {"score": 55, "confidence": 0.5, "thesis": "mixed signal"},
    }
    base.update(overrides)
    return base


def _mock_llm_verdict(verdict: str, *, confidence: float = 0.8) -> AsyncMock:
    return AsyncMock(
        return_value=(
            {
                "verdict": verdict,
                "confidence": confidence,
                "rationale": "Clean setup, clear thesis.",
                "bull_case": "Strong momentum and volume confirm the move.",
                "bear_case": "A gap down would invalidate this quickly.",
                "risk_level": 3,
                "conviction_level": 4,
            },
            False,
        )
    )


# ─────────────────────────────────────────────────────────────────────
# strategy_fit_node's instrument gate — purely additive
# ─────────────────────────────────────────────────────────────────────

_CLEARS_FIT_FLOOR_FEATURES = {
    "technicals": {
        "trend_regime": "uptrend",
        "dma20_pct": 2.0,
        "dma50_pct": 4.0,
        "rsi_14": 55.0,
        "atr_14": 2.0,
        "volume_ratio_20d": 1.6,
    },
    "quant": {
        "ret_252d_pct": 20.0,
        "ret_63d_pct": 10.0,
        "ret_21d_pct": 6.0,
        "sharpe": 1.0,
        "atr_zscore": 0.5,
        "realized_vol_pct": 25.0,
        "corr_benchmark": 0.5,
        "price_zscore_20": 0.5,
        "donchian_pct": 90.0,
    },
}


async def test_instrument_set_when_flag_and_preference_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    state = {
        "symbol": "NVDA",
        "context": dict(_CLEARS_FIT_FLOOR_FEATURES),
        "instrument_preference": "option",
    }
    out = await strategy_fit_mod.strategy_fit_node(state)
    assert out["selected_strategy"] is not None  # sanity: this fixture DOES win
    assert out["instrument"] == "option"


async def test_instrument_absent_without_allow_options_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_OPTIONS", raising=False)
    state = {
        "symbol": "NVDA",
        "context": dict(_CLEARS_FIT_FLOOR_FEATURES),
        "instrument_preference": "option",
    }
    out = await strategy_fit_mod.strategy_fit_node(state)
    assert out["selected_strategy"] is not None
    assert "instrument" not in out


async def test_instrument_absent_without_a_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    state = {"symbol": "NVDA", "context": dict(_CLEARS_FIT_FLOOR_FEATURES)}
    out = await strategy_fit_mod.strategy_fit_node(state)
    assert out["selected_strategy"] is not None
    assert "instrument" not in out


def _no_fit_features() -> dict:
    """Every precondition maximally unsatisfied — copied from
    ``test_council_mock.py``'s ``_featureless()`` fixture (proven, in that
    existing passing test, to score every strategy below MIN_FIT_TO_TRADE).
    A sparser dict is NOT safe to assume HOLDs: most component checks
    default to NEUTRAL (0.5) when their input is missing, which is already
    above the 0.45 floor, so an under-specified fixture can spuriously win."""
    return {
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


async def test_instrument_absent_on_hold_even_with_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HOLD must stay untouched by the new branch — it returns before
    this node ever reaches the winner-found return statement."""
    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    state = {
        "symbol": "NOFIT",
        "context": _no_fit_features(),
        "instrument_preference": "option",
    }
    out = await strategy_fit_mod.strategy_fit_node(state)
    assert out["selected_strategy"] is None
    assert out["final_action"] == "HOLD"
    assert "instrument" not in out


# ─────────────────────────────────────────────────────────────────────
# drafter_node's options branch
# ─────────────────────────────────────────────────────────────────────


async def test_options_drafter_produces_long_call_with_is_option_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drafter_mod, "complete_json", _mock_llm_verdict("BUY"))
    monkeypatch.setattr(
        drafter_mod,
        "_fetch_option_candidates",
        AsyncMock(return_value=(_quote(contract_type="call"),)),
    )

    out = await drafter_mod.drafter_node(
        _drafter_state(selected_direction="long"), llm=object()
    )

    assert out["final_action"] == "BUY"
    p = out["proposal"]
    assert p is not None
    assert p["is_option"] is True
    assert p["option_action"] == "buy_to_open"
    assert p["contract_type"] == "call"
    assert p["occ_symbol"] == "AAPL_TEST_CALL"
    assert p["strike"] == 250.0
    assert p["multiplier"] == 100
    assert p["side"] == "BUY"
    assert p["qty"] >= 1
    # Alpaca has no options bracket — must not promise an exit plan it
    # cannot keep.
    assert p["stop_loss"] is None
    assert p["target_price"] is None
    # days_to_earnings flows options_context -> drafter -> proposal, not
    # re-fetched.
    assert p["days_to_earnings"] == 12
    # Extra snapshot fields an options risk rule needs at execution time.
    assert p["bid"] == 3.00
    assert p["ask"] == 3.20
    assert p["open_interest"] == 500
    assert p["volume"] == 200
    assert p["implied_volatility"] == 0.30


async def test_options_drafter_bearish_thesis_buys_a_put_but_side_stays_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bearish ("short") thesis buys a PUT — it never opens a short
    option leg. `side` must stay "BUY" regardless of thesis direction;
    see drafter._draft_option_proposal's docstring for why this is
    load-bearing for the short-side risk rules."""
    monkeypatch.setattr(drafter_mod, "complete_json", _mock_llm_verdict("SELL"))
    monkeypatch.setattr(
        drafter_mod,
        "_fetch_option_candidates",
        AsyncMock(
            return_value=(_quote(contract_type="put", occ_symbol="AAPL_TEST_PUT", delta=-0.55),)
        ),
    )

    out = await drafter_mod.drafter_node(
        _drafter_state(selected_direction="short"), llm=object()
    )

    p = out["proposal"]
    assert p is not None
    assert p["contract_type"] == "put"
    assert p["direction"] == "short"
    assert p["side"] == "BUY"
    assert p["opens_short"] is False
    assert out["final_action"] == "BUY"


async def test_options_drafter_no_liquid_contract_holds_with_named_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drafter_mod, "complete_json", _mock_llm_verdict("BUY"))
    # Every candidate is illiquid (OI below the floor) -> select_contract
    # empties at the liquidity stage.
    monkeypatch.setattr(
        drafter_mod,
        "_fetch_option_candidates",
        AsyncMock(return_value=(_quote(open_interest=5),)),
    )

    out = await drafter_mod.drafter_node(
        _drafter_state(selected_direction="long"), llm=object()
    )

    assert out["final_action"] == "HOLD"
    assert out["proposal"] is None
    assert "No liquid option contract found" in out["drafter_rationale"]
    assert "no_liquid_contract" in out["drafter_rationale"]


async def test_options_drafter_no_candidates_at_all_holds_not_equity_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain fetch returning nothing (e.g. broker.alpaca.
    list_option_contracts not implemented yet) must HOLD with a named
    reason — never silently fall back to the equity ATR path."""
    monkeypatch.setattr(drafter_mod, "complete_json", _mock_llm_verdict("BUY"))
    monkeypatch.setattr(
        drafter_mod, "_fetch_option_candidates", AsyncMock(return_value=())
    )

    out = await drafter_mod.drafter_node(
        _drafter_state(selected_direction="long"), llm=object()
    )

    assert out["final_action"] == "HOLD"
    assert out["proposal"] is None  # never silently equity-sized
    assert "no_candidates" in out["drafter_rationale"]


async def test_options_drafter_zero_qty_holds_with_sizer_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget that can't afford one contract must HOLD, not size to 0
    contracts silently — mirrors the equity path's own qty<1 conversion."""
    monkeypatch.setattr(drafter_mod, "complete_json", _mock_llm_verdict("BUY"))
    # $100k equity * 1% default options_max_premium_pct = $1,000 budget.
    # A $500 ask x100 multiplier = $50,000/contract -- unaffordable.
    monkeypatch.setattr(
        drafter_mod,
        "_fetch_option_candidates",
        AsyncMock(return_value=(_quote(bid=498.0, ask=500.0),)),
    )

    out = await drafter_mod.drafter_node(
        _drafter_state(selected_direction="long"), llm=object()
    )

    assert out["final_action"] == "HOLD"
    assert out["proposal"] is None
    assert "Sizer returned 0 contracts" in out["drafter_rationale"]


async def test_options_instrument_flag_absent_takes_the_equity_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard on the branch condition itself: without
    state["instrument"] == "option", the options helpers must never be
    called at all — the equity ATR path runs exactly as before."""
    monkeypatch.setattr(drafter_mod, "complete_json", _mock_llm_verdict("BUY"))
    candidates_fetch = AsyncMock(return_value=(_quote(),))
    monkeypatch.setattr(drafter_mod, "_fetch_option_candidates", candidates_fetch)

    state = _drafter_state(selected_direction="long")
    del state["instrument"]
    out = await drafter_mod.drafter_node(state, llm=object())

    candidates_fetch.assert_not_called()
    assert out["proposal"] is not None
    assert out["proposal"].get("is_option", False) is False
    assert out["proposal"]["sizing_method"] in ("atr", "fallback_pct")


# ─────────────────────────────────────────────────────────────────────
# Cross-boundary regression: an options proposal must never accidentally
# satisfy short_requires_stop. Precedent cited explicitly per the task
# notes — the short-position work spent 5 commits on exactly this class
# of bug (a field one rule needs silently missing from what another code
# path produces).
# ─────────────────────────────────────────────────────────────────────


def test_options_proposal_side_buy_never_satisfies_short_requires_stop() -> None:
    """An options proposal carries stop_price=None (Alpaca has no bracket
    for options — see drafter._draft_option_proposal's docstring) and
    side="BUY" (forced, even for a bearish/put thesis). If ``side`` were
    ever allowed to drift to "SELL" for a bearish thesis (the equity
    path's own convention), `opens_short` would read it as opening an
    unbounded short with no stop and `short_requires_stop` would veto it
    for the WRONG reason, masking the real gap (risk_officer_node is not
    yet options-aware — see this task's report). Pinning `side="BUY"`
    here is what keeps that whole rule family a no-op for options, as
    intended, rather than an accidental (and confusing) veto.
    """
    proposal = RiskProposal(
        symbol="AAPL",
        side=Side.BUY,
        qty=2,
        estimated_notional=640.0,
        last_price=250.0,
        confidence=0.8,
        stop_price=None,
    )
    context = RiskContext(account_equity=100_000.0, cash=100_000.0, buying_power=100_000.0)
    caps = RiskCaps.from_env()

    assert opens_short(proposal, context) is False
    assert short_requires_stop(proposal, context, caps) is None
