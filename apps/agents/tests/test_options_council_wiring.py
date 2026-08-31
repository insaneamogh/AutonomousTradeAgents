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
