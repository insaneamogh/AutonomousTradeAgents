# IMPL 1 — `llm.py` full tool support

**Implementation spec. Build this first — IMPL 2 depends on it.**
Written 2026-08-31 by `ID:MODEL1REAL`. Est **4h**.

---

## 0. Current state (verified — do not re-derive)

`apps/agents/trading_agents/llm.py`:

- `LLM.complete(*, system, user, model, max_tokens, cache_system, council_run_id, agent_decision_id, user_id) -> LLMResponse`
- The API call is `client.messages.create(model, max_tokens, system=system_blocks, messages=[{"role":"user","content":user}], temperature=...)`
- **Line 153:** `text = msg.content[0].text if msg.content else ""` — takes block **[0]** and assumes `.text`
- `LLMResponse` is a frozen dataclass: `text`, `model`, 4 token counters. No `stop_reason`, no blocks.
- `complete_json(llm, ...) -> tuple[dict|None, bool]` is a **module-level function**, one retry.
- `Model` is a plain class of str constants: `OPUS`, `SONNET`, `HAIKU`.
- MOCK mode when `ANTHROPIC_API_KEY` unset. `_mock_response` (**line 311**) branches on
  `"you are the <role>"` in `system[:120].lower()`.
- `cost_ledger.py:199` `infer_role_from_system_prompt` branches on `system[:160].lower()`.
- Installed SDK: **`anthropic==0.109.1`** — full tool support, the wrapper just doesn't expose it.
- **Zero hits repo-wide** for `tool_use|tools=|tool_choice|input_schema`.

---

## 1. Commit 1 — block walk (shared path, land alone)

### 1.1 New types

```python
@dataclass(frozen=True)
class ToolCall:
    """One `tool_use` block the model emitted."""
    id: str            # echo back as tool_use_id on the result
    name: str
    input: dict[str, Any]


def _extract_blocks(msg: Any) -> tuple[str, tuple[ToolCall, ...]]:
    """Walk every content block instead of assuming content[0] is text.

    The old `msg.content[0].text` broke on any response whose FIRST block
    is not text — which is every tool-using response, and also a plain
    text response that happens to lead with a thinking block. Behaviour is
    identical for today's five council nodes (one text block in, the same
    string out); it is strictly more robust for everything else.
    """
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in (msg.content or []):
        btype = getattr(block, "type", None)
        if btype == "text":
            texts.append(block.text)
        elif btype == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input or {})))
    return "\n".join(texts), tuple(calls)
```

### 1.2 `LLMResponse` gains two fields

```python
tool_calls: tuple[ToolCall, ...] = ()
stop_reason: str | None = None
```

Both default, so every existing construction site keeps compiling.

### 1.3 Replace line 153

```python
text, tool_calls = _extract_blocks(msg)
```
and pass `tool_calls=tool_calls, stop_reason=getattr(msg, "stop_reason", None)` into
the `LLMResponse`.

### 1.4 Gate

**Full suite green before starting commit 2.** This touches the hot path of all five
council nodes. If anything regresses it must be visible here, not tangled with new
functionality.

---

## 2. Commit 2 — `complete_tools()`

> 🚨 **A sibling method, NOT an overload of `complete()`.** `complete()` hardcodes
> `messages=[{"role":"user","content":user}]` and five nodes ride it. A tool loop needs
> caller-supplied `messages` so it can append assistant + `tool_result` turns.
> Refactoring `complete()`'s signature is how you break the equity council.

```python
async def complete_tools(
    self,
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str = Model.SONNET,
    max_tokens: int = 2048,
    tool_choice: dict[str, Any] | None = None,
    cache_system: bool = True,
    council_run_id: str | None = None,
    agent_decision_id: str | None = None,
    user_id: str | None = None,
) -> LLMResponse:
    """One tool-enabled turn. The LOOP lives in the caller, not here —
    this method is a single request/response so the caller owns how many
    rounds it is willing to pay for.
    """
```

Body mirrors `complete()` exactly, plus:

```python
kwargs: dict[str, Any] = {
    "model": model,
    "max_tokens": max_tokens,
    "system": system_blocks,
    "messages": messages,
    "temperature": float(os.environ.get("LLM_TEMPERATURE", "0.0")),
    "tools": tools,
}
if tool_choice is not None:
    kwargs["tool_choice"] = tool_choice
msg = await client.messages.create(**kwargs)
```

Cost-ledger recording identical to `complete()` — same `_record_to_ledger` call.

### 2.1 MOCK mode

```python
if self.mock:
    # Flatten the last user-ish text out of `messages` and hand it to the
    # existing mock. It returns TEXT ONLY and never a tool_use block.
    return self._mock_response(system=system, user=_flatten(messages), model=model)
```

> 🚨 **The mock must never emit a `tool_use` block.** The loop in §3 terminates when a
> response has no tool calls. A mock that emits one loops until `max_rounds`, every
> test, forever.

---

## 3. Commit 3 — the loop helper

`apps/agents/trading_agents/llm_loop.py`:

```python
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
    """Run until the model stops calling tools, or `max_rounds` is hit.

    Returns (final_response, transcript). `dispatch` is the GUARDED tool
    runner — it must NEVER raise; a refusal comes back as a dict with
    `is_error: True` (see IMPL_OPTIONS_AGENTS.md §3).
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    transcript: list[dict[str, Any]] = []
    resp = None

    for _round in range(max_rounds):
        resp = await llm.complete_tools(
            system=system, messages=messages, tools=tools,
            model=model, max_tokens=max_tokens, **ledger_kwargs,
        )
        if not resp.tool_calls:
            return resp, transcript

        # Echo the assistant turn back verbatim — the API requires the
        # tool_use blocks to be present in history for the tool_result
        # blocks that follow to be valid.
        messages.append({"role": "assistant", "content": _assistant_blocks(resp)})

        results = []
        for call in resp.tool_calls:
            out = await dispatch(call)          # guarded; never raises
            transcript.append({"tool": call.name, "input": call.input, "output": out})
            results.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps(out.get("content", out)),
                "is_error": bool(out.get("is_error", False)),
            })
        messages.append({"role": "user", "content": results})

    # Budget exhausted. Return whatever we have; the caller decides — for
    # an options agent that means HOLD, never "assume it meant yes".
    return resp, transcript
```

**`_assistant_blocks(resp)`** rebuilds the assistant content from `resp.text` +
`resp.tool_calls`. Keep it a pure function so it is unit-testable.

---

## 4. Registering a new role — BOTH places or it fails silently

A new agent role needs a branch in **both**:

| File | Line | Matches |
|---|---|---|
| `llm.py::_mock_response` | ~311 | `"you are the <role>"` in `system[:120].lower()` |
| `cost_ledger.py::infer_role_from_system_prompt` | ~199 | same phrase in `system[:160].lower()` |

Miss the first → MOCK returns a generic `{score, confidence, thesis}` and your parser
silently gets the wrong shape. Miss the second → every call logs as `"unknown"` in the
cost ledger. **Neither raises.** Both need an explicit test (§5).

The system prompt therefore **must begin** `You are the <Role Name>`.

---

## 5. Tests — revert-check matrix

`apps/agents/tests/test_llm_tools.py`

| Test | Break this to make it fail |
|---|---|
| `test_block_walk_extracts_text_from_a_non_first_block` | Restore `content[0].text` |
| `test_block_walk_collects_tool_calls` | Return `()` for calls |
| `test_existing_complete_is_behaviour_identical` | — regression guard; assert a single-text-block response yields the same string as before |
| **`test_mock_never_emits_tool_use`** | Make the mock emit one. **Most important — an infinite loop otherwise.** |
| `test_loop_terminates_when_no_tool_calls` | Always append and re-request |
| **`test_loop_bounded_at_max_rounds`** | Raise/remove the cap |
| `test_dispatch_error_becomes_is_error_result` | Let `dispatch` raise |
| `test_tool_result_echoes_tool_use_id` | Emit a wrong/missing id — the API rejects the next turn |
| `test_new_role_resolves_in_mock_response` | Remove the `_mock_response` branch |
| `test_new_role_resolves_in_cost_ledger` | Remove the `infer_role` branch |

**Baseline: 969 passed, 11 skipped.** `git stash`, re-run, `git stash pop` before
attributing any failure to your change.

---

## 6. Where you will go wrong

1. **Overloading `complete()`.** Five nodes ride it.
2. **A mock that emits `tool_use`.** Infinite loop in every test.
3. **Putting the loop inside `complete_tools()`.** The caller must own the round budget.
4. **Letting `dispatch` raise.** One bad tool call aborts the whole pass.
5. **Forgetting to echo the assistant turn** before the `tool_result` — the API rejects
   the next request with a confusing 400.
6. **Registering the role in only one of the two places.** Both fail silently.
7. **Landing commits 1 and 2 together.** Commit 1 touches every node; keep it isolated.

---

*Next: [`IMPL_OPTIONS_AGENTS.md`](IMPL_OPTIONS_AGENTS.md)*
