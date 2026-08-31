"""Round-trip loop for tool-enabled LLM turns.

``LLM.complete_tools()`` is a single request/response — the loop that keeps
calling it until the model stops asking for tools (or a round budget runs
out) lives here instead, so the CALLER owns how many rounds it is willing
to pay for. See ``docs/IMPL_LLM_TOOLS.md`` §3.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from trading_agents.llm import LLM, LLMResponse, Model, ToolCall

logger = logging.getLogger("agents.llm_loop")


def _assistant_blocks(resp: LLMResponse) -> list[dict[str, Any]]:
    """Rebuild the assistant turn's content from ``resp.text`` +
    ``resp.tool_calls``. Kept pure so it is unit-testable on its own.
    """
    blocks: list[dict[str, Any]] = []
    if resp.text:
        blocks.append({"type": "text", "text": resp.text})
    for call in resp.tool_calls:
        blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.input})
    return blocks


async def run_tool_loop(
    llm: LLM,
    *,
    system: str,
    user: str,
    tools: list[dict[str, Any]],
    dispatch: Callable[[ToolCall], Awaitable[dict[str, Any]]],
    model: str = Model.SONNET,
    max_rounds: int = 3,
    max_tokens: int = 2048,
    **ledger_kwargs: Any,
) -> tuple[LLMResponse, list[dict[str, Any]]]:
    """Run until the model stops calling tools, or ``max_rounds`` is hit.

    Returns ``(final_response, transcript)``. ``dispatch`` is the GUARDED
    tool runner — it must never raise; a refusal comes back as a dict with
    ``is_error: True`` (see IMPL_OPTIONS_AGENTS.md §3). A raise is still
    caught here and converted to the same ``is_error`` shape rather than
    aborting the round trip — belt-and-suspenders for §6's trap #4 ("one
    bad tool call aborts the whole pass"), on top of the contract already
    asked of ``dispatch``.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    transcript: list[dict[str, Any]] = []
    resp: LLMResponse | None = None

    for _round in range(max_rounds):
        resp = await llm.complete_tools(
            system=system,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            **ledger_kwargs,
        )
        if not resp.tool_calls:
            return resp, transcript

        # Echo the assistant turn back verbatim — the API requires the
        # tool_use blocks to be present in history for the tool_result
        # blocks that follow to be valid.
        messages.append({"role": "assistant", "content": _assistant_blocks(resp)})

        results: list[dict[str, Any]] = []
        for call in resp.tool_calls:
            try:
                out = await dispatch(call)
            except Exception as exc:
                logger.warning(
                    "run_tool_loop: dispatch raised for tool %r — treating as is_error: %s",
                    call.name,
                    exc,
                )
                out = {"is_error": True, "content": f"tool dispatch raised: {exc}"}
            transcript.append({"tool": call.name, "input": call.input, "output": out})
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(out.get("content", out)),
                    "is_error": bool(out.get("is_error", False)),
                }
            )
        messages.append({"role": "user", "content": results})

    # Budget exhausted. Return whatever we have; the caller decides — for
    # an options agent that means HOLD, never "assume it meant yes".
    assert resp is not None  # max_rounds >= 1 in every real caller
    return resp, transcript
