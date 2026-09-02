"""The seam that makes run_options_agents reachable from a real pass.

Before this, `run_options_agents` was fully built, fully tested, and called
from NOWHERE but its own tests -- `USE_OPTIONS_AGENT` was read by no
production code, so every options pass silently went through the shared
equity council instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from trading_agents.nodes.options_council import (
    _contract_funnel,
    _denials,
    _traded,
    options_agent_enabled,
    options_council_node,
)


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "NVDA",
        "horizon": "short",
        "instrument": "option",
        "selected_strategy": "momentum",
        "context": {},
    }
    base.update(over)
    return base


# ── the gate ──────────────────────────────────────────────────────────


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_OPTIONS_AGENT", raising=False)
    assert options_agent_enabled(_state()) is False


def test_enabled_only_for_an_options_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_OPTIONS_AGENT", "1")
    assert options_agent_enabled(_state()) is True
    # An equity pass must never route here.
    assert options_agent_enabled(_state(instrument="equity")) is False


def test_requires_a_strategy_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """strategy_fit stays the free deterministic gate — a no-setup symbol
    must not reach two LLM agents."""
    monkeypatch.setenv("USE_OPTIONS_AGENT", "1")
    assert options_agent_enabled(_state(selected_strategy=None)) is False


# ── transcript readers ────────────────────────────────────────────────


def test_traded_ignores_a_denied_call() -> None:
    """The guard NEVER raises, so a refusal arrives through the same
    transcript. 'called the tool' and 'a trade happened' are different
    questions."""
    t = ({"tool": "open_option_trade",
          "output": {"is_error": True, "content": {"denied": "illiquid_contract"}}},)
    assert _traded(t) is None


def test_traded_returns_the_successful_open() -> None:
    t = ({"tool": "open_option_trade",
          "output": {"is_error": False,
                     "content": {"decision_id": "d1", "occ_symbol": "NVDA…C", "qty": 3}}},)
    got = _traded(t)
    assert got is not None and got["decision_id"] == "d1"


def test_denials_are_named() -> None:
    t = (
        {"tool": "open_option_trade",
         "output": {"is_error": True, "content": {"denied": "max_premium_pct"}}},
        {"tool": "get_iv_rank", "output": {"is_error": False, "content": {}}},
    )
    assert _denials(t) == ["open_option_trade:max_premium_pct"]


def test_contract_funnel_reads_a_denied_opens_content() -> None:
    """guard.py now folds `contract_funnel` into a denial's `content`
    (`dispatch_tool_call`) whenever `select_contract` actually ran — this
    is the reader on the other end."""
    t = ({"tool": "open_option_trade",
          "output": {"is_error": True,
                     "content": {"denied": "no_delta_in_band",
                                 "contract_funnel": {"counts": {"total": 1, "delta_band": 0},
                                                      "rejection_reason": "no_delta_in_band",
                                                      "selected_occ": None}}}},)
    funnel = _contract_funnel(t)
    assert funnel is not None
    assert funnel["rejection_reason"] == "no_delta_in_band"


def test_contract_funnel_is_none_when_the_tool_was_never_called() -> None:
    """No `open_option_trade` entry at all (agents disagreed, or neither
    resolved a direction) must not fabricate a funnel — matches
    `nodes/drafter.py`'s own "claiming a funnel would be fabricating a
    stage that never ran" rule for the equity-council options path."""
    t = ({"tool": "get_iv_rank", "output": {"is_error": False, "content": {}}},)
    assert _contract_funnel(t) is None
    assert _contract_funnel(()) is None


def test_contract_funnel_reads_a_successful_opens_content() -> None:
    t = ({"tool": "open_option_trade",
          "output": {"is_error": False,
                     "content": {"decision_id": "d9",
                                 "contract_funnel": {"counts": {"total": 1},
                                                      "rejection_reason": None,
                                                      "selected_occ": "NVDA…C"}}}},)
    funnel = _contract_funnel(t)
    assert funnel is not None
    assert funnel["selected_occ"] == "NVDA…C"


# ── the node ──────────────────────────────────────────────────────────


class _Result:
    def __init__(self, *, proceed: bool, transcript: tuple = (), reason: str = "agreed") -> None:
        class _V:
            direction, conviction, thesis, degraded = "long", 0.6, "NVDA breaks 190 in 3 weeks.", False
        class _R:
            pass
        r = _R()
        r.proceed, r.reason, r.direction, r.conviction = proceed, reason, "long", 0.6
        self.bull, self.bear, self.resolution = _V(), _V(), r
        self.trade_response, self.tool_transcript = None, transcript


class _FakeVerdict:
    def __init__(
        self, allow: bool, reason: str | None = None, payload: dict[str, Any] | None = None
    ) -> None:
        self.allow, self.reason, self.payload = allow, reason, payload


class _FakeGuard:
    """Stands in for ToolGuard. `preflight_allow=False` simulates the
    account-level pre-flight refusing before any model call;
    `chain_allow=False` simulates the CHAIN pre-flight doing the same.
    `payload` (default None, matching a preflight that carries nothing —
    e.g. a gate refusal) is threaded onto whichever verdict actually
    denies, so a test can pin exactly what options_council_node does with
    it without needing a real guard.py call underneath."""

    def __init__(
        self,
        preflight_allow: bool = True,
        reason: str | None = None,
        *,
        chain_allow: bool = True,
        chain_reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._allow, self._reason = preflight_allow, reason
        self._chain_allow, self._chain_reason = chain_allow, chain_reason
        self._payload = payload

    async def preflight_can_open(self, **_: Any) -> _FakeVerdict:
        payload = self._payload if not self._allow else None
        return _FakeVerdict(self._allow, self._reason, payload)

    async def preflight_chain_is_tradeable(self, **_: Any) -> _FakeVerdict:
        payload = self._payload if not self._chain_allow else None
        return _FakeVerdict(self._chain_allow, self._chain_reason, payload)


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    result: Any,
    *,
    guard: Any = None,
    calls: list | None = None,
) -> None:
    import trading_agents.options.agents as agents_mod
    import trading_agents.options.tools.guard as guard_mod

    async def _fake(*a: Any, **k: Any) -> Any:
        if calls is not None:
            calls.append(1)
        return result

    monkeypatch.setattr(agents_mod, "run_options_agents", _fake)
    monkeypatch.setattr(
        guard_mod, "ToolGuard", lambda *a, **k: (guard if guard is not None else _FakeGuard())
    )


async def test_a_successful_trade_marks_the_row_already_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trade tool writes its own agent_decisions row on the SAME
    council_run_id runtime would use. Without this flag the council's row
    lands on top and erases the fill, order id and risk checks."""
    t = ({"tool": "open_option_trade",
          "output": {"is_error": False,
                     "content": {"decision_id": "d9", "occ_symbol": "NVDA…C",
                                 "qty": 3, "checks_passed": ["min_dte"]}}},)
    _patch(monkeypatch, _Result(proceed=True, transcript=t))
    out = await options_council_node(_state(), llm=object())
    assert out["decision_row_written"] is True
    assert out["decision_id"] == "d9"
    assert out["final_action"] == "BUY"
    assert out["risk_approved"] is True


async def test_a_denied_trade_holds_and_names_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = ({"tool": "open_option_trade",
          "output": {"is_error": True, "content": {"denied": "illiquid_contract"}}},)
    _patch(monkeypatch, _Result(proceed=True, transcript=t))
    out = await options_council_node(_state(), llm=object())
    assert out["final_action"] == "HOLD"
    assert not out.get("decision_row_written")
    assert "illiquid_contract" in out["drafter_rationale"]


async def test_a_denied_trade_threads_the_contract_funnel_onto_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap this closes: `runtime._reasoning_block` persists whatever
    `state["contract_funnel"]` is under `reasoning.contract_funnel` — this
    node used to never set that key at all, so every options HOLD through
    the live Bull/Bear/guard path persisted a bare `null` there regardless
    of whether `select_contract` ran and named a real rejection.

    Revert-checked: removing this node's `"contract_funnel": _contract_
    funnel(result.tool_transcript)` line (reverting to the pre-fix state,
    where the key is simply absent from `out`) makes this fail with a
    KeyError on `out["contract_funnel"]`. Confirmed, then restored.
    """
    t = ({"tool": "open_option_trade",
          "output": {"is_error": True,
                     "content": {"denied": "no_liquid_contract",
                                 "contract_funnel": {"counts": {"total": 4, "liquidity": 0},
                                                      "rejection_reason": "no_liquid_contract",
                                                      "selected_occ": None}}}},)
    _patch(monkeypatch, _Result(proceed=True, transcript=t))
    out = await options_council_node(_state(), llm=object())
    assert out["final_action"] == "HOLD"
    funnel = out["contract_funnel"]
    assert funnel["rejection_reason"] == "no_liquid_contract"
    assert funnel["counts"]["total"] == 4


async def test_agents_disagreeing_never_fabricates_a_contract_funnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tool call happened at all — `select_contract` never ran, so
    there is no funnel to report. Matches nodes/drafter.py's own rule for
    an equity pass: claiming a funnel here would fabricate a stage that
    never ran."""
    _patch(monkeypatch, _Result(proceed=False, reason="agents_disagree"))
    out = await options_council_node(_state(), llm=object())
    assert out["contract_funnel"] is None


async def test_disagreement_holds_with_the_named_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, _Result(proceed=False, reason="agents_disagree"))
    out = await options_council_node(_state(), llm=object())
    assert out["final_action"] == "HOLD"
    assert "agents_disagree" in out["drafter_rationale"]


async def test_an_exception_holds_rather_than_killing_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trading_agents.options.agents as agents_mod
    import trading_agents.options.tools.guard as guard_mod

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(agents_mod, "run_options_agents", _boom)
    monkeypatch.setattr(guard_mod, "ToolGuard", lambda *a, **k: object())
    out = await options_council_node(_state(), llm=object())
    assert out["final_action"] == "HOLD"
    assert out["proposal"] is None


# ── runtime must not overwrite the trade tool's row ───────────────────


async def test_runtime_skips_its_own_write_when_a_row_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trade tool persists its OWN agent_decisions row keyed on the
    same council_run_id runtime uses. A second write lands a pass summary
    on top of a real executed trade and erases the fill, order id and
    checks_passed."""
    import trading_agents.runtime as rt

    recorded: list[Any] = []

    class _Log:
        async def record(self, entry: Any) -> Any:
            recorded.append(entry)
            return entry

    async def _fake_graph(state: Any, **kw: Any) -> Any:
        return {
            **state,
            "final_action": "BUY",
            "risk_approved": True,
            "decision_row_written": True,
            "decision_id": "already-written",
            "proposal": None,
        }

    monkeypatch.setattr(rt, "run_graph", _fake_graph)
    out = await rt.run_council(
        symbol="NVDA", llm=rt.LLM(api_key=None), decision_log=_Log()
    )
    assert recorded == [], "runtime wrote a second row over the trade tool's"
    assert out["decision_id"] == "already-written"


async def test_runtime_still_writes_on_a_normal_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the branch above — the ordinary path must be
    completely unaffected."""
    import trading_agents.runtime as rt

    recorded: list[Any] = []

    class _Log:
        async def record(self, entry: Any) -> Any:
            recorded.append(entry)
            entry.id = "fresh"
            return entry

    async def _fake_graph(state: Any, **kw: Any) -> Any:
        return {**state, "final_action": "HOLD", "risk_approved": False}

    monkeypatch.setattr(rt, "run_graph", _fake_graph)
    await rt.run_council(symbol="NVDA", llm=rt.LLM(api_key=None), decision_log=_Log())
    assert len(recorded) == 1


async def test_preflight_refusal_spends_zero_llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the pre-flight: when an ACCOUNT-level gate
    already makes a trade impossible, `run_options_agents` — and therefore
    both Sonnet debate calls and the trade hop — must never run at all.

    Measured 2026-09-01: 48 of 293 options runs were refused
    `max_total_premium_pct`, a portfolio-level fact independent of the
    symbol, each after ~3 paid Sonnet calls."""
    calls: list = []
    _patch(
        monkeypatch,
        _Result(proceed=True),
        guard=_FakeGuard(preflight_allow=False, reason="max_total_premium_pct"),
        calls=calls,
    )

    out = await options_council_node(_state(), llm=object())

    assert calls == [], "run_options_agents must not run after a pre-flight refusal"
    assert out["final_action"] == "HOLD"
    assert out["tool_denials"] == ["preflight:max_total_premium_pct"]
    assert "max_total_premium_pct" in out["drafter_rationale"]


async def test_preflight_allowing_still_runs_the_normal_paid_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A book under the cap must be unaffected — the pre-flight is a
    short-circuit, never a new refusal reason of its own."""
    calls: list = []
    _patch(monkeypatch, _Result(proceed=True), guard=_FakeGuard(True), calls=calls)

    out = await options_council_node(_state(), llm=object())

    assert calls == [1]
    assert out["final_action"] == "HOLD"  # no trade in the transcript
    # This fixture agrees to trade and then emits an EMPTY transcript,
    # which is precisely the tool-calling failure `_attempted_trade`
    # exists to name. It reads as an incidental detail of the fixture and
    # is not: in production the same shape means the model never asked to
    # trade, and reporting that as an ordinary stand-down is what hid a
    # dead desk. The pre-flight's own contract — it adds no refusal of its
    # own — is what `calls == [1]` above asserts.
    assert out.get("tool_denials") == ["open_option_trade:no_call_emitted"]
    assert "tool-calling failure" in out["drafter_rationale"]


async def test_a_guard_without_a_preflight_degrades_to_the_normal_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open, at the node boundary too: an old/stubbed guard with no
    `preflight_can_open` must not stop the desk trading."""
    calls: list = []
    _patch(monkeypatch, _Result(proceed=True), guard=object(), calls=calls)

    out = await options_council_node(_state(), llm=object())

    assert calls == [1]
    assert out["final_action"] == "HOLD"


async def test_an_untradeable_chain_spends_zero_llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answers "why is a paid council pass first?" — it no longer is.
    `select_contract` normally runs inside the tool guard, after both debate
    calls and the trade hop, so a chain that could never produce a tradeable
    contract cost ~3 model calls to discover. CME261016P00270000 was exactly
    that shape and cost $1,200."""
    calls: list = []
    _patch(
        monkeypatch,
        _Result(proceed=True),
        guard=_FakeGuard(chain_allow=False, chain_reason="illiquid_chain"),
        calls=calls,
    )

    out = await options_council_node(
        _state(selected_direction="long"), llm=object()
    )

    assert calls == [], "run_options_agents must not run for an untradeable chain"
    assert out["final_action"] == "HOLD"
    assert out["tool_denials"] == ["preflight:illiquid_chain"]


async def test_a_tradeable_chain_still_runs_the_paid_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    _patch(
        monkeypatch,
        _Result(proceed=True),
        guard=_FakeGuard(chain_allow=True),
        calls=calls,
    )

    await options_council_node(_state(selected_direction="long"), llm=object())

    assert calls == [1]


async def test_preflight_refusal_threads_risk_veto_rule_onto_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the 2026-09-02 visibility gap: a preflight
    refusal that names a REAL RiskDecision.veto_rule
    (`max_total_premium_pct`) must reach `out["risk_veto_rule"]`, or
    ghost_service.build_veto_ledger's `risk_veto_rule IS NOT NULL` filter
    silently excludes it — the more this pre-flight saves, the blinder the
    Refusal Ledger got to options, until this fix."""
    _patch(
        monkeypatch,
        _Result(proceed=True),
        guard=_FakeGuard(
            preflight_allow=False, reason="max_total_premium_pct",
            payload={"risk_veto_rule": "max_total_premium_pct"},
        ),
    )

    out = await options_council_node(_state(), llm=object())

    assert out["risk_veto_rule"] == "max_total_premium_pct"
    assert out["risk_approved"] is False


async def test_preflight_gate_refusal_does_not_fabricate_a_risk_veto_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same fix: an operator/environment gate
    (`market_closed`) carries no payload, so `risk_veto_rule` must stay
    None rather than defaulting to the bare reason string — that string is
    not a RiskDecision.veto_rule, no evaluate() ever ran."""
    _patch(
        monkeypatch,
        _Result(proceed=True),
        guard=_FakeGuard(preflight_allow=False, reason="market_closed"),
    )

    out = await options_council_node(_state(), llm=object())

    assert out["risk_veto_rule"] is None


async def test_chain_preflight_refusal_threads_the_contract_funnel_onto_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The funnel/veto distinction must survive the full node, not just
    the guard: illiquid_chain/no_liquid_contract carry a contract_funnel,
    never a risk_veto_rule (they're selection's vocabulary, not the risk
    engine's)."""
    funnel = {
        "counts": {"total": 1},
        "rejection_reason": "illiquid_chain",
        "selected_occ": None,
    }
    _patch(
        monkeypatch,
        _Result(proceed=True),
        guard=_FakeGuard(
            chain_allow=False, chain_reason="illiquid_chain",
            payload={"contract_funnel": funnel},
        ),
    )

    out = await options_council_node(_state(selected_direction="long"), llm=object())

    assert out["contract_funnel"] == funnel
    assert out["risk_veto_rule"] is None


async def test_preflight_refusal_never_claims_checks_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct lock on RiskDecision.checks_passed's own documented contract
    (packages/engine/engine/risk/types.py): a rule that self-gates out —
    or, here, was never reached because evaluate() never ran at all — must
    not be recorded as having passed. Neither preflight runs evaluate(),
    so this branch must never populate risk_checks_passed."""
    _patch(
        monkeypatch,
        _Result(proceed=True),
        guard=_FakeGuard(
            preflight_allow=False, reason="max_total_premium_pct",
            payload={"risk_veto_rule": "max_total_premium_pct"},
        ),
    )

    out = await options_council_node(_state(), llm=object())

    assert not out.get("risk_checks_passed")


def test_the_options_agents_default_to_sonnet() -> None:
    """This defaulted to Haiku for a few hours on 2026-09-02, on the argument
    that the guard re-runs the whole risk stack regardless of which model
    asked, so a weaker model costs SELECTION but never RISK CONTROL.

    True, and beside the point. The trade hop has to emit a well-formed
    `open_option_trade` call, and a hop that never emits one produces no
    trade, no error and no ledger row — the same observable as a market
    with nothing worth trading. Cost is bounded by the day/hour symbol
    caps and the two pre-flights, which cut spend by debating fewer
    symbols rather than by debating them worse."""
    from trading_agents.llm import Model
    from trading_agents.options.agents import _options_model

    assert _options_model() == Model.SONNET


def test_the_options_model_is_revertible_without_a_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_agents.llm import Model
    from trading_agents.options.agents import _options_model

    monkeypatch.setenv("OPTIONS_AGENT_MODEL", "haiku")
    assert _options_model() == Model.HAIKU, "an explicit cost-constrained run"

    monkeypatch.setenv("OPTIONS_AGENT_MODEL", "nonsense")
    assert _options_model() == Model.SONNET, "an unknown value must not reach the API"


async def test_a_deliberate_stand_down_reads_differently_from_a_dead_tool_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two no-trade outcomes must never collapse into one message.

    Both end the pass flat, and until `_attempted_trade` existed both said
    "Agents agreed but chose not to open a position." They have opposite
    remedies — one is the desk working, the other is the model failing to
    drive the tool loop — so a shared wording makes a broken options
    council invisible for as long as nobody reads the transcripts.
    """
    from trading_agents.nodes.options_council import _attempted_trade

    # The model asked and the guard refused: an ATTEMPT. The system worked.
    denied = ({"tool": "open_option_trade",
               "input": {},
               "output": {"is_error": True, "content": {"denied": "illiquid_contract"}}},)
    assert _attempted_trade(denied) is True

    # The model only looked around and never asked: NOT an attempt.
    browsed = ({"tool": "get_option_snapshot",
                "input": {},
                "output": {"content": {}}},)
    assert _attempted_trade(browsed) is False
    assert _attempted_trade(()) is False
