"""Tests for `llm.py` tool support: the block walk, `complete_tools()`, and
the `run_tool_loop()` round-trip helper (`llm_loop.py`).

See `docs/IMPL_LLM_TOOLS.md` §5 for the revert-check matrix this file
implements — each test's docstring names which behavior it pins and, where
relevant, notes what was actually reverted and observed to fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from trading_agents.cost_ledger import reset_cost_ledger_for_tests
from trading_agents.llm import LLM, LLMResponse, Model, ToolCall, _extract_blocks


@pytest.fixture(autouse=True)
def _reset_ledger() -> None:
    # This file constructs real-mode `LLM` instances that write to the
    # process-singleton cost ledger — reset so no test's row count depends
    # on collection order relative to test_cost_ledger.py.
    reset_cost_ledger_for_tests()


# ─────────────────────────────────────────────────────────────────────
# Fake Anthropic message/content-block shapes — mirror the real SDK's
# attribute surface (`.type`, `.text`, `.id`, `.name`, `.input`) without
# depending on it, so these tests exercise the exact `getattr` paths
# `_extract_blocks` uses.
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _FakeMessage:
    content: list[Any] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Any = None


# ─────────────────────────────────────────────────────────────────────
# Commit 1 — the block walk
# ─────────────────────────────────────────────────────────────────────


def test_block_walk_extracts_text_from_a_non_first_block() -> None:
    """The old `msg.content[0].text` broke on any response whose FIRST
    block isn't text. Put a tool_use block first — the extracted text must
    still be the text block's content, not an AttributeError (tool_use
    blocks have no `.text`).

    Revert-checked: temporarily changed `_extract_blocks` to
    `return (msg.content[0].text if msg.content else "", ())` — this test
    then failed with `AttributeError: '_FakeToolUseBlock' object has no
    attribute 'text'`, confirming it actually exercises the fixed path.
    Restored afterwards.
    """
    msg = _FakeMessage(
        content=[
            _FakeToolUseBlock(id="toolu_1", name="get_price", input={"symbol": "NVDA"}),
            _FakeTextBlock(text="Here is my analysis."),
        ]
    )
    text, calls = _extract_blocks(msg)
    assert text == "Here is my analysis."
    assert len(calls) == 1


def test_block_walk_collects_tool_calls() -> None:
    """Every `tool_use` block must come back as a `ToolCall` with id/name/
    input preserved — not an empty tuple."""
    msg = _FakeMessage(
        content=[
            _FakeTextBlock(text="Let me check the price."),
            _FakeToolUseBlock(id="toolu_42", name="get_price", input={"symbol": "NVDA"}),
        ]
    )
    text, calls = _extract_blocks(msg)
    assert text == "Let me check the price."
    assert calls == (ToolCall(id="toolu_42", name="get_price", input={"symbol": "NVDA"}),)


def test_block_walk_handles_multiple_text_blocks_and_empty_content() -> None:
    """Multiple text blocks join with a newline; no content returns ("", ())
    — matching the old `if msg.content else ""` guard exactly."""
    msg = _FakeMessage(content=[_FakeTextBlock(text="first"), _FakeTextBlock(text="second")])
    text, calls = _extract_blocks(msg)
    assert text == "first\nsecond"
    assert calls == ()

    empty = _FakeMessage(content=[])
    text2, calls2 = _extract_blocks(empty)
    assert text2 == ""
    assert calls2 == ()


def test_existing_complete_is_behaviour_identical() -> None:
    """Regression guard for the five existing council nodes: a normal
    single-text-block response must yield exactly the string
    `content[0].text` used to produce, with no tool calls.
    """
    msg = _FakeMessage(content=[_FakeTextBlock(text='{"score": 64.0}')])
    text, calls = _extract_blocks(msg)
    assert text == msg.content[0].text
    assert calls == ()


async def test_complete_threads_tool_calls_and_stop_reason_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring check for IMPL_LLM_TOOLS.md §1.3: `complete()`'s real-mode
    path must actually call `_extract_blocks` and carry `tool_calls` /
    `stop_reason` onto the returned `LLMResponse` — not just the text.
    """
    llm = LLM(api_key="sk-test-not-a-placeholder-0000000000000000")
    assert llm.mock is False

    fake_msg = _FakeMessage(
        content=[
            _FakeTextBlock(text="thinking out loud"),
            _FakeToolUseBlock(id="toolu_9", name="get_quote", input={"symbol": "SPY"}),
        ],
        stop_reason="tool_use",
    )

    class _FakeMessages:
        async def create(self, **kwargs: Any) -> _FakeMessage:
            return fake_msg

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(llm, "_get_client", lambda: _FakeClient())

    resp = await llm.complete(system="You are the Router.", user="Ticker: SPY")

    assert resp.text == "thinking out loud"
    assert resp.tool_calls == (ToolCall(id="toolu_9", name="get_quote", input={"symbol": "SPY"}),)
    assert resp.stop_reason == "tool_use"


def test_llm_response_new_fields_default_so_old_construction_sites_compile() -> None:
    """`tool_calls`/`stop_reason` must default — every pre-existing
    `LLMResponse(text=..., model=...)` call site (mock + tests) must keep
    compiling unchanged."""
    resp = LLMResponse(text="hi", model=Model.SONNET)
    assert resp.tool_calls == ()
    assert resp.stop_reason is None
