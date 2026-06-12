"""Langfuse tracing — no-op safety + degraded/fail recording.

The load-bearing guarantee: with no Langfuse keys, tracing is a hard no-op
and the council behaves byte-identically. We also pin that complete_json
drives the right generation outcome (succeed / degrade / fail) regardless
of whether a real client is attached — the council must never break because
of telemetry.
"""

from __future__ import annotations

import json

import pytest

from trading_agents import tracing
from trading_agents.llm import LLMResponse, complete_json


@pytest.fixture(autouse=True)
def _no_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    tracing.reset_for_tests()
    yield
    tracing.reset_for_tests()


def test_tracing_disabled_without_keys() -> None:
    assert tracing.tracing_enabled() is False


def test_council_trace_is_a_noop_without_keys() -> None:
    # Must not raise, must yield a usable handle.
    with tracing.council_trace(symbol="NVDA", horizon="short") as trace:
        trace.set_output(output={"final_action": "BUY"}, metadata={"degraded_nodes": []})
    tracing.flush()  # also a no-op


def test_agent_generation_noop_handles_all_outcomes() -> None:
    with tracing.agent_generation(role="router", model="m", system="s", user="u") as gen:
        gen.succeed(output={"ok": True}, usage={"input": 1, "output": 2}, cost=0.001)
        gen.degrade(output={}, status="x")
        gen.fail(status="y")


class _FakeLLM:
    """Deterministic stand-in exposing the complete() surface."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.calls = 0

    async def complete(self, **_kw: object) -> LLMResponse:
        text = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        return LLMResponse(text=text, model="claude-haiku-4-5-20251001",
                           input_tokens=10, output_tokens=5)


async def test_complete_json_success_first_try() -> None:
    llm = _FakeLLM([json.dumps({"score": 60})])
    data, degraded = await complete_json(llm, system="You are the Router", user="u")
    assert data == {"score": 60}
    assert degraded is False
    assert llm.calls == 1


async def test_complete_json_degrades_then_parses() -> None:
    llm = _FakeLLM(["not json", json.dumps({"score": 55})])
    data, degraded = await complete_json(llm, system="You are the Router", user="u")
    assert data == {"score": 55}
    assert degraded is True
    assert llm.calls == 2  # original + one retry


async def test_complete_json_fails_after_retry() -> None:
    llm = _FakeLLM(["nope", "still nope"])
    data, degraded = await complete_json(llm, system="You are the Router", user="u")
    assert data is None
    assert degraded is True
    assert llm.calls == 2
