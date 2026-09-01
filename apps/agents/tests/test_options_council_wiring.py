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


def _patch(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    import trading_agents.options.agents as agents_mod
    import trading_agents.options.tools.guard as guard_mod

    async def _fake(*a: Any, **k: Any) -> Any:
        return result

    monkeypatch.setattr(agents_mod, "run_options_agents", _fake)
    monkeypatch.setattr(guard_mod, "ToolGuard", lambda *a, **k: object())


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
