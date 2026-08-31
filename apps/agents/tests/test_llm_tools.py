"""Tests for `llm.py` tool support: the block walk, `complete_tools()`, and
the `run_tool_loop()` round-trip helper (`llm_loop.py`).

See `docs/IMPL_LLM_TOOLS.md` §5 for the revert-check matrix this file
implements — each test's docstring names which behavior it pins and, where
relevant, notes what was actually reverted and observed to fail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from trading_agents.cost_ledger import infer_role_from_system_prompt, reset_cost_ledger_for_tests
from trading_agents.llm import (
    LLM,
    LLMResponse,
    Model,
    ToolCall,
    _extract_blocks,
    _flatten,
    _mock_response,
)
from trading_agents.llm_loop import _assistant_blocks, run_tool_loop


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


# ─────────────────────────────────────────────────────────────────────
# Commit 2 — complete_tools()
# ─────────────────────────────────────────────────────────────────────

_A_TOOL: dict[str, Any] = {
    "name": "get_quote",
    "description": "Get the latest quote for a symbol.",
    "input_schema": {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    },
}


@pytest.mark.parametrize(
    "system_prompt",
    [
        "You are the Router on a quant desk.",
        "You are the Technical Analyst.",
        "You are the Proposal Drafter. The only non-HOLD verdict allowed is BUY.",
        "You are an entirely unregistered role nobody wrote a branch for.",
    ],
)
async def test_mock_never_emits_tool_use(system_prompt: str) -> None:
    """The single most important invariant in this file: `run_tool_loop`
    (llm_loop.py) terminates its round trip on `not resp.tool_calls`. A
    mock that ever emitted a `tool_use` block would loop until
    `max_rounds` on every test — including these — that exercises the
    tools path, since MOCK mode is what CI and offline runs use.

    Revert-checked: temporarily made the mock branch of `complete_tools`
    return `LLMResponse(text="{}", model=model,
    tool_calls=(ToolCall(id="t1", name="get_quote", input={}),))` instead
    of calling `_mock_response`. All 4 parametrizations of this test then
    failed (`assert (ToolCall(...),) == ()`), confirming it actually
    guards the invariant. Restored after.
    """
    llm = LLM(api_key=None)
    assert llm.mock is True

    resp = await llm.complete_tools(
        system=system_prompt,
        messages=[{"role": "user", "content": "Ticker: NVDA"}],
        tools=[_A_TOOL],
    )
    assert resp.tool_calls == ()


async def test_complete_tools_mock_reuses_mock_response_shape() -> None:
    """Proves the mock path actually flows through `_mock_response` (via
    `_flatten`), not some degenerate empty stand-in — the role-specific
    shape (`analyst_subset` for the Router) must still be present."""
    llm = LLM(api_key=None)
    resp = await llm.complete_tools(
        system="You are the Router on a quant desk.",
        messages=[{"role": "user", "content": "Ticker: NVDA"}],
        tools=[_A_TOOL],
    )
    body = json.loads(resp.text)
    assert "analyst_subset" in body


def test_flatten_prefers_last_user_message() -> None:
    """A second-round tool loop appends a `tool_result` block list as the
    newest user turn — `_flatten` must pick THAT one, not the original
    first-round string, so the mock's regex-based extraction (ticker,
    required side) sees the freshest signal."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Ticker: NVDA"},
        {"role": "assistant", "content": [{"type": "text", "text": "checking..."}]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"price": 100.5}',
                    "is_error": False,
                }
            ],
        },
    ]
    assert _flatten(messages) == '{"price": 100.5}'


def test_flatten_returns_empty_string_when_no_user_message() -> None:
    assert _flatten([{"role": "assistant", "content": "hello"}]) == ""
    assert _flatten([]) == ""


async def test_complete_tools_real_mode_passes_tools_and_messages_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring check for IMPL_LLM_TOOLS.md §2: the real-mode path must send
    the CALLER-SUPPLIED `messages` and `tools` verbatim (not the
    single-user-turn shape `complete()` hardcodes), include `tool_choice`
    only when given, and still thread `tool_calls`/`stop_reason` through.
    """
    llm = LLM(api_key="sk-test-not-a-placeholder-0000000000000000")
    assert llm.mock is False

    fake_msg = _FakeMessage(
        content=[_FakeToolUseBlock(id="toolu_7", name="get_quote", input={"symbol": "NVDA"})],
        stop_reason="tool_use",
    )
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs: Any) -> _FakeMessage:
            captured.update(kwargs)
            return fake_msg

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(llm, "_get_client", lambda: _FakeClient())

    caller_messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Ticker: NVDA"},
        {"role": "assistant", "content": [{"type": "text", "text": "checking..."}]},
    ]
    resp = await llm.complete_tools(
        system="You are the Bull Analyst.",
        messages=caller_messages,
        tools=[_A_TOOL],
        tool_choice={"type": "auto"},
    )

    assert captured["messages"] is caller_messages
    assert captured["tools"] == [_A_TOOL]
    assert captured["tool_choice"] == {"type": "auto"}
    assert resp.tool_calls == (
        ToolCall(id="toolu_7", name="get_quote", input={"symbol": "NVDA"}),
    )
    assert resp.stop_reason == "tool_use"


async def test_complete_tools_real_mode_omits_tool_choice_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tool_choice` must be omitted entirely (not sent as `None`) when the
    caller doesn't pass one — the Anthropic API distinguishes 'absent' from
    an explicit null."""
    llm = LLM(api_key="sk-test-not-a-placeholder-0000000000000000")
    fake_msg = _FakeMessage(content=[_FakeTextBlock(text="ok")])
    captured: dict[str, Any] = {}

    class _FakeMessages:
        async def create(self, **kwargs: Any) -> _FakeMessage:
            captured.update(kwargs)
            return fake_msg

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(llm, "_get_client", lambda: _FakeClient())

    await llm.complete_tools(
        system="You are the Bull Analyst.",
        messages=[{"role": "user", "content": "Ticker: NVDA"}],
        tools=[_A_TOOL],
    )

    assert "tool_choice" not in captured


# ─────────────────────────────────────────────────────────────────────
# Commit 3 — run_tool_loop() (llm_loop.py)
# ─────────────────────────────────────────────────────────────────────


class _StubLLM:
    """A controllable `complete_tools()` double: one canned `LLMResponse`
    per call, plus every call's kwargs recorded for assertions.

    Raises if asked for more rounds than were canned, rather than
    repeating the last response forever — so a loop that fails to respect
    `max_rounds` (or one that keeps looping when it shouldn't) surfaces as
    an immediate, readable `AssertionError` instead of a silent hang.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete_tools(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if len(self.calls) > len(self._responses):
            raise AssertionError(
                f"complete_tools called {len(self.calls)} times but only "
                f"{len(self._responses)} response(s) were canned — the "
                "round budget was not respected"
            )
        return self._responses[len(self.calls) - 1]


async def test_loop_terminates_when_no_tool_calls() -> None:
    """A response with no tool calls must return immediately — no second
    request, and `dispatch` never invoked.

    Revert-checked: temporarily removed the `if not resp.tool_calls: return`
    early-out (always appended the assistant turn and looped again). The
    stub only cans one response, so the forced second round raised the
    stub's own "round budget was not respected" AssertionError, and
    separately `_dispatch` (which asserts it is never called) would also
    have failed. Restored after.
    """
    resp_no_tools = LLMResponse(text='{"verdict": "HOLD"}', model=Model.SONNET)
    stub = _StubLLM([resp_no_tools])

    async def _dispatch(call: ToolCall) -> dict[str, Any]:
        raise AssertionError("dispatch must not be called when there are no tool calls")

    final_resp, transcript = await run_tool_loop(
        cast(LLM, stub),
        system="You are the Bull Analyst.",
        user="Ticker: NVDA",
        tools=[_A_TOOL],
        dispatch=_dispatch,
    )

    assert final_resp is resp_no_tools
    assert transcript == []
    assert len(stub.calls) == 1


async def test_loop_bounded_at_max_rounds() -> None:
    """A model that keeps calling tools forever must be cut off at
    `max_rounds` — never a `max_rounds + 1`th request.

    Revert-checked: temporarily changed `for _round in range(max_rounds)`
    to `for _round in range(max_rounds + 1)`. This test then failed with
    the stub's "complete_tools called 3 times but only 2 response(s) were
    canned" AssertionError — proving it actually pins the cap rather than
    just happening to pass. Restored after.
    """
    calls = tuple(
        ToolCall(id=f"toolu_{i}", name="get_quote", input={"symbol": "NVDA"}) for i in range(2)
    )
    responses = [LLMResponse(text="", model=Model.SONNET, tool_calls=(c,)) for c in calls]
    stub = _StubLLM(responses)

    async def _dispatch(call: ToolCall) -> dict[str, Any]:
        return {"content": {"price": 100.0}}

    final_resp, transcript = await run_tool_loop(
        cast(LLM, stub),
        system="You are the Bull Analyst.",
        user="Ticker: NVDA",
        tools=[_A_TOOL],
        dispatch=_dispatch,
        max_rounds=2,
    )

    assert len(stub.calls) == 2
    assert len(transcript) == 2
    assert final_resp is responses[-1]


async def test_dispatch_error_becomes_is_error_result() -> None:
    """A `dispatch` that raises must not abort the loop — the exception is
    caught and turned into an `is_error: True` tool_result, and the round
    trip continues normally to completion.

    Revert-checked: temporarily removed the `try/except` around
    `await dispatch(call)` in `llm_loop.run_tool_loop`. This test then
    failed with an uncaught `RuntimeError: boom` propagating out of
    `run_tool_loop` instead of the loop completing normally. Restored
    after.
    """
    call = ToolCall(id="toolu_1", name="open_option_trade", input={"symbol": "NVDA"})
    resp_with_call = LLMResponse(text="", model=Model.SONNET, tool_calls=(call,))
    resp_done = LLMResponse(text='{"verdict": "HOLD"}', model=Model.SONNET)
    stub = _StubLLM([resp_with_call, resp_done])

    async def _raising_dispatch(call: ToolCall) -> dict[str, Any]:
        raise RuntimeError("boom")

    final_resp, transcript = await run_tool_loop(
        cast(LLM, stub),
        system="You are the Bull Analyst.",
        user="Ticker: NVDA",
        tools=[_A_TOOL],
        dispatch=_raising_dispatch,
    )

    assert final_resp is resp_done
    assert len(transcript) == 1
    assert transcript[0]["output"]["is_error"] is True

    second_round_messages = stub.calls[1]["messages"]
    tool_result_turn = second_round_messages[-1]
    assert tool_result_turn["role"] == "user"
    assert tool_result_turn["content"][0]["is_error"] is True
    assert tool_result_turn["content"][0]["tool_use_id"] == "toolu_1"


async def test_tool_result_echoes_tool_use_id() -> None:
    """The `tool_result` block's `tool_use_id` must match the `ToolCall`'s
    `id` exactly — a wrong or missing id makes the API reject the next
    turn with a 400."""
    call = ToolCall(id="toolu_specific_789", name="get_quote", input={"symbol": "NVDA"})
    resp_with_call = LLMResponse(text="checking", model=Model.SONNET, tool_calls=(call,))
    resp_done = LLMResponse(text="done", model=Model.SONNET)
    stub = _StubLLM([resp_with_call, resp_done])

    async def _dispatch(call: ToolCall) -> dict[str, Any]:
        return {"content": {"price": 123.45}}

    await run_tool_loop(
        cast(LLM, stub),
        system="You are the Bull Analyst.",
        user="Ticker: NVDA",
        tools=[_A_TOOL],
        dispatch=_dispatch,
    )

    second_round_messages = stub.calls[1]["messages"]
    tool_result_turn = second_round_messages[-1]
    assert tool_result_turn["content"][0]["tool_use_id"] == "toolu_specific_789"


async def test_loop_forwards_ledger_kwargs_to_complete_tools() -> None:
    """`**ledger_kwargs` (council_run_id/user_id/agent_decision_id) must
    reach `complete_tools()` unchanged — the loop is not the place those
    get dropped."""
    stub = _StubLLM([LLMResponse(text="ok", model=Model.SONNET)])

    async def _dispatch(call: ToolCall) -> dict[str, Any]:
        return {"content": "n/a"}

    await run_tool_loop(
        cast(LLM, stub),
        system="You are the Bull Analyst.",
        user="Ticker: NVDA",
        tools=[_A_TOOL],
        dispatch=_dispatch,
        council_run_id="run-1",
        user_id="user-1",
    )

    assert stub.calls[0]["council_run_id"] == "run-1"
    assert stub.calls[0]["user_id"] == "user-1"


def test_assistant_blocks_rebuilds_text_and_tool_use() -> None:
    call = ToolCall(id="toolu_1", name="get_quote", input={"symbol": "NVDA"})
    resp = LLMResponse(text="checking the price", model=Model.SONNET, tool_calls=(call,))
    assert _assistant_blocks(resp) == [
        {"type": "text", "text": "checking the price"},
        {"type": "tool_use", "id": "toolu_1", "name": "get_quote", "input": {"symbol": "NVDA"}},
    ]


def test_assistant_blocks_omits_empty_text() -> None:
    call = ToolCall(id="toolu_1", name="get_quote", input={"symbol": "NVDA"})
    resp = LLMResponse(text="", model=Model.SONNET, tool_calls=(call,))
    assert _assistant_blocks(resp) == [
        {"type": "tool_use", "id": "toolu_1", "name": "get_quote", "input": {"symbol": "NVDA"}},
    ]


# ─────────────────────────────────────────────────────────────────────
# Role registration — the dual-branch pattern (§4)
#
# IMPL_LLM_TOOLS.md §4: a new agent role needs a branch in BOTH
# `_mock_response` and `infer_role_from_system_prompt`, or it fails
# silently (wrong shape from the mock / "unknown" in the cost ledger,
# neither raises). This spec does not add a new role itself — that is
# IMPL_OPTIONS_AGENTS.md's job — so these exercise the pattern against
# every role registered TODAY, parametrized, as the template the next
# role addition must not break: removing either branch for any one row
# below fails that row's test.
# ─────────────────────────────────────────────────────────────────────

_GENERIC_FALLBACK_BODY = {"score": 50.0, "confidence": 0.2, "thesis": "MOCK: generic neutral response."}

# (system prompt, cost-ledger role tag, a key present ONLY in that role's
# `_mock_response` body — i.e. absent from the generic fallback).
_ROLE_CASES: tuple[tuple[str, str, str], ...] = (
    ("You are the Router on a quant desk.", "router", "analyst_subset"),
    ("You are the Technical Analyst.", "technical", "citations"),
    ("You are the Fundamental Analyst.", "fundamental", "citations"),
    ("You are the Macro Analyst.", "macro", "citations"),
    ("You are the Strategy Selector.", "selector", "strategy"),
    (
        "You are the Proposal Drafter. The only non-HOLD verdict allowed is BUY.",
        "drafter",
        "verdict",
    ),
    ("You are the Reflection Agent.", "reflection", "lessons"),
)


@pytest.mark.parametrize("system_prompt,expected_role,marker_key", _ROLE_CASES)
def test_new_role_resolves_in_mock_response(
    system_prompt: str, expected_role: str, marker_key: str
) -> None:
    """Every registered role must produce its own role-shaped mock body,
    not the generic {score, confidence, thesis} fallback `_mock_response`
    falls back to for an unrecognized prompt.

    Revert-checked (router row): temporarily commented out the
    `"you are the router" in role_line` branch in `_mock_response`. The
    router-row parametrization then failed — the body fell through to the
    generic fallback (no `analyst_subset` key, and equal to
    `_GENERIC_FALLBACK_BODY`). Restored after.
    """
    resp = _mock_response(system=system_prompt, user="Ticker: NVDA", model=Model.SONNET)
    body = json.loads(resp.text)
    assert marker_key in body, f"{expected_role} branch did not fire — got fallback body {body}"
    assert body != _GENERIC_FALLBACK_BODY


@pytest.mark.parametrize("system_prompt,expected_role,marker_key", _ROLE_CASES)
def test_new_role_resolves_in_cost_ledger(
    system_prompt: str, expected_role: str, marker_key: str
) -> None:
    """The same system prompt must resolve to the matching role tag in the
    cost ledger — never `"unknown"`.

    Revert-checked (router row): temporarily commented out the
    `"you are the router" in line: return "router"` branch in
    `infer_role_from_system_prompt`. The router-row parametrization then
    failed (`'unknown' != 'router'`). Restored after.
    """
    assert infer_role_from_system_prompt(system_prompt) == expected_role
