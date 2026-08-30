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

import warnings
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from engine.options.selection import ContractQuote
from engine.risk import RiskCaps, RiskContext, RiskProposal, Side, opens_short
from engine.risk.rules.short_requires_stop import short_requires_stop
from trading_agents.nodes import drafter as drafter_mod
from trading_agents.nodes import strategy_fit as strategy_fit_mod

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="websockets.legacy is deprecated", category=DeprecationWarning
    )
    import alpaca.data.historical.option as alpaca_option_data
    import alpaca.trading.client as alpaca_trading_client

from alpaca.data.models.quotes import Quote
from alpaca.data.models.snapshots import OptionsGreeks, OptionsSnapshot
from alpaca.data.models.trades import Trade
from alpaca.trading.models import OptionContract

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
# strategy_fit_node's two distinct HOLD rationales — genuinely-no-fit vs.
# too-thin-to-call-tradable (docs/PLAN_AGGRESSIVE_PROFILE.md §4)
# ─────────────────────────────────────────────────────────────────────


async def test_hold_rationale_names_thin_evidence_not_the_fit_floor() -> None:
    """An empty/near-empty feature dict must not read like "best was X at
    0.60, holding" — that phrasing is only correct for a genuinely
    sub-floor score, and 0.60 clears MIN_FIT_TO_TRADE. The node must say
    the data was too thin instead, and the fit block must carry
    ``usable_features: False`` + a reason for the audit row/UI.
    """
    state = {"symbol": "EMPTY", "context": {}}
    out = await strategy_fit_mod.strategy_fit_node(state)

    assert out["selected_strategy"] is None
    assert out["final_action"] == "HOLD"
    assert "clears the fit floor" not in out["selector_rationale"]
    assert "too thin to trade" in out["selector_rationale"]

    fit = out["strategy_fit"]
    assert fit["usable_features"] is False
    assert fit["unusable_reason"]
    assert fit["winner"] is None
    assert fit["ranked"], "the nominal ranking must still be there for the audit row"


async def test_hold_rationale_still_names_the_fit_floor_for_real_data() -> None:
    """The pre-existing, genuinely-no-fit case (rich technicals + quant,
    every precondition unsatisfied) must be completely unaffected — same
    wording, ``usable_features: True``, no ``unusable_reason`` key at all.
    """
    state = {"symbol": "NOFIT", "context": _no_fit_features()}
    out = await strategy_fit_mod.strategy_fit_node(state)

    assert out["selected_strategy"] is None
    assert out["final_action"] == "HOLD"
    assert "clears the fit floor" in out["selector_rationale"]

    fit = out["strategy_fit"]
    assert fit["usable_features"] is True
    assert "unusable_reason" not in fit


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
    # Always LIMIT with a real limit_price — never MARKET, and never a
    # LIMIT order with no price. The broker layer builds a LimitOrderRequest
    # straight off limit_price with no None-guard (unlike STOP/STOP_LIMIT,
    # which do raise on a missing price), so an unset value here would
    # reach Alpaca as an invalid order — this regression-guards exactly
    # that bug.
    assert p["order_type"] == "LIMIT"
    assert p["limit_price"] == p["ask"]
    assert p["limit_price"] is not None
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


# ─────────────────────────────────────────────────────────────────────
# The end-to-end test that would have caught the chain-fetch inertness
# bug — every test above monkeypatches _fetch_option_candidates directly
# with idealized data. This one does NOT: it patches only the two real
# Alpaca SDK client CLASSES with realistic model_construct fixtures, and
# lets the real _fetch_option_candidates -> engine.options.contracts.
# fetch_option_candidates -> broker.alpaca path run for real. See the
# build-log entries for the bug this proves is fixed.
# ─────────────────────────────────────────────────────────────────────


def _occ_symbol(underlying: str, expiry: date, contract_type: str, strike: float) -> str:
    yymmdd = expiry.strftime("%y%m%d")
    cp = "C" if contract_type == "call" else "P"
    strike_digits = f"{round(strike * 1000):08d}"
    return f"{underlying}{yymmdd}{cp}{strike_digits}"


async def test_drafter_options_path_end_to_end_through_real_alpaca_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setattr(drafter_mod, "complete_json", _mock_llm_verdict("BUY", confidence=0.8))

    # Dynamic expiry (not a fixed date): the real code computes `now` at
    # call time via datetime.now(UTC), and select_contract's own 21-45 DTE
    # window is checked against THAT clock, not a fixture-frozen one.
    expiry = (datetime.now(UTC) + timedelta(days=30)).date()
    occ = _occ_symbol("AAPL", expiry, "call", 250.0)

    snapshot = OptionsSnapshot.model_construct(
        symbol=occ,
        latest_quote=Quote.model_construct(
            symbol=occ, timestamp=_NOW, bid_price=3.10, ask_price=3.30
        ),
        latest_trade=Trade.model_construct(symbol=occ, timestamp=_NOW, price=3.20, size=25.0),
        implied_volatility=0.28,
        # confidence=0.8 >= the high-conviction threshold -> delta band
        # [0.45, 0.65] (see engine.options.selection's own docstring).
        greeks=OptionsGreeks.model_construct(delta=0.55, gamma=0.0, rho=0.0, theta=0.0, vega=0.0),
    )
    contract = OptionContract.model_construct(symbol=occ, open_interest="500")

    class FakeOptionHistoricalDataClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_option_chain(self, request: object) -> dict[str, OptionsSnapshot]:
            return {occ: snapshot}

    class FakeTradingClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_option_contracts(self, request: object) -> object:
            return type("Resp", (), {"option_contracts": [contract]})()

    monkeypatch.setattr(
        alpaca_option_data, "OptionHistoricalDataClient", FakeOptionHistoricalDataClient
    )
    monkeypatch.setattr(alpaca_trading_client, "TradingClient", FakeTradingClient)

    out = await drafter_mod.drafter_node(_drafter_state(selected_direction="long"), llm=object())

    assert out["final_action"] == "BUY"
    p = out["proposal"]
    assert p is not None
    assert p["is_option"] is True
    assert p["occ_symbol"] == occ
    assert p["contract_type"] == "call"
    assert p["strike"] == 250.0
    assert p["bid"] == 3.10
    assert p["ask"] == 3.30
    assert p["limit_price"] == 3.30
    assert p["implied_volatility"] == 0.28
    assert p["open_interest"] == 500
    assert p["volume"] == 25
    assert p["qty"] >= 1


# ─────────────────────────────────────────────────────────────────────
# The watchlist -> options reachability seam
#
# `instrument_preference` had exactly one production caller (the /agent/run
# route) and no client ever sent it, so no scheduled or scanner-triggered
# run could produce an option. `user_watchlist.asset_class` was persisted
# and surfaced in the UI from the start but never read back. These pin the
# wiring that closes that gap.
# ─────────────────────────────────────────────────────────────────────


async def test_run_one_forwards_option_instrument_to_the_council(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_agents.jobs import daily_cron

    captured: dict[str, object] = {}

    async def _fake_run_council(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"final_action": "HOLD", "proposal": None}

    monkeypatch.setattr(daily_cron, "run_council", _fake_run_council)
    monkeypatch.setattr(
        daily_cron, "_already_decided_today", AsyncMock(return_value=False)
    )

    await daily_cron._run_one(
        "00000000-0000-0000-0000-000000000001", "NVDA", object(),
        force=True, feature_provider=None, push_tasks=[], instrument="option",
    )
    assert captured["instrument_preference"] == "option"


async def test_run_one_defaults_to_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_agents.jobs import daily_cron

    captured: dict[str, object] = {}

    async def _fake_run_council(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"final_action": "HOLD", "proposal": None}

    monkeypatch.setattr(daily_cron, "run_council", _fake_run_council)
    monkeypatch.setattr(
        daily_cron, "_already_decided_today", AsyncMock(return_value=False)
    )

    await daily_cron._run_one(
        "00000000-0000-0000-0000-000000000001", "NVDA", object(),
        force=True, feature_provider=None, push_tasks=[],
    )
    assert captured["instrument_preference"] == "equity"


async def test_main_routes_each_symbol_by_its_own_asset_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mixed watchlist must route per-row, not all-or-nothing."""
    from trading_agents.jobs import daily_cron

    seen: list[tuple[str, str]] = []

    async def _fake_run_one(_uid, symbol, _llm, *, instrument="equity", **_kw):
        seen.append((symbol, instrument))
        return {"symbol": symbol, "skipped": False}

    monkeypatch.setattr(daily_cron, "_run_one", _fake_run_one)
    monkeypatch.setattr(daily_cron, "resolve_feature_provider", lambda **_kw: None)

    await daily_cron.main(
        "00000000-0000-0000-0000-000000000001",
        ["SPY", "MSFT"],
        force=True,
        skip_calendar_gate=True,
        skip_ghost_eval=True,
        skip_reflect=True,
        instrument_by_symbol={"SPY": "option"},  # MSFT absent -> equity
    )
    assert seen == [("SPY", "option"), ("MSFT", "equity")]
