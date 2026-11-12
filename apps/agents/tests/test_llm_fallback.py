"""GLM unreachable -> Claude Sonnet 5.5.

Operator decision, 2026-11-12: "switch the model to sonnet 5.5 when glm is
not reachable". Before this, a Z.ai outage made every council node raise,
every node abstain, and the desk HOLD all day with nothing in the log but
transport errors.

The two providers are different model generations, so the fallback is not
"send the same request somewhere else": Sonnet 5.5 answers a non-default
``temperature`` with a 400, and thinks by default against budgets that were
sized for a model that does not. ``request_shape`` owns that difference and
is tested here against the real request each fake client receives.

Nothing here touches the network. The SDK clients are faked; the errors are
the SDK's own exception classes, built the way the SDK builds them, because
``unreachable_reason`` classifies by type and status and a hand-rolled
exception would not exercise it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx
import pytest

from trading_agents import llm as llm_mod
from trading_agents.cost_ledger import (
    _PRICES,
    compute_cost_usd,
    get_cost_ledger,
    reset_cost_ledger_for_tests,
)
from trading_agents.llm import (
    FALLBACK_MODEL,
    LLM,
    Model,
    fallback_model,
    fallback_status,
    request_shape,
    reset_fallback_for_tests,
    unreachable_reason,
)
from trading_agents.llm_loop import run_tool_loop

_REQUEST = httpx.Request("POST", "https://api.z.ai/api/anthropic/v1/messages")

_STATUS_ERRORS: dict[int, str] = {
    400: "BadRequestError",
    401: "AuthenticationError",
    403: "PermissionDeniedError",
    404: "NotFoundError",
    413: "RequestTooLargeError",
    422: "UnprocessableEntityError",
    429: "RateLimitError",
    500: "InternalServerError",
    503: "InternalServerError",
    529: "OverloadedError",
}


def _status_error(code: int) -> anthropic.APIStatusError:
    cls = getattr(anthropic, _STATUS_ERRORS[code], anthropic.APIStatusError)
    return cls(f"http {code}", response=httpx.Response(code, request=_REQUEST), body=None)


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_REQUEST)


@dataclass
class _Block:
    text: str = ""
    type: str = "text"
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Usage:
    input_tokens: int = 1000
    output_tokens: int = 100
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Message:
    content: list[Any]
    stop_reason: str | None = "end_turn"
    usage: Any = field(default_factory=_Usage)
    stop_details: Any = None


class _FakeClient:
    """Stands in for ``AsyncAnthropic``. ``script`` is consumed one entry
    per call: an exception instance is raised, anything else is returned."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.messages = self

    def with_options(self, **kwargs: Any) -> _FakeClient:
        self.options.append(kwargs)
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else _Message([_Block("{}")])
        if isinstance(item, BaseException):
            raise item
        return item


def _ok(text: str = '{"ok": true}') -> _Message:
    return _Message([_Block(text)])


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_cost_ledger_for_tests()
    reset_fallback_for_tests()
    for name in (
        "LLM_FALLBACK_MODEL", "LLM_FALLBACK_ENABLED", "LLM_FALLBACK_COOLDOWN_SECONDS",
        "LLM_EFFORT", "LLM_TEMPERATURE", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
        "GLM_MODEL_SONNET", "GLM_MODEL_HAIKU", "GLM_MODEL_OPUS", "ZAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    reset_fallback_for_tests()


def _glm_with_fallback(
    monkeypatch: pytest.MonkeyPatch,
    *,
    primary: list[Any],
    fallback: list[Any] | None = None,
) -> tuple[LLM, _FakeClient, _FakeClient]:
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fallback-key")
    client = LLM()
    p, f = _FakeClient(primary), _FakeClient(fallback or [])
    monkeypatch.setattr(client, "_get_client", lambda: p)
    monkeypatch.setattr(client, "_get_fallback_client", lambda: f)
    return client, p, f


# ─────────────────────────────────────────────────────────────────────
# The fallback itself
# ─────────────────────────────────────────────────────────────────────


async def test_an_unreachable_glm_is_answered_by_sonnet_5_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, primary, fallback = _glm_with_fallback(
        monkeypatch, primary=[_connection_error()], fallback=[_ok('{"score": 61}')]
    )
    assert client.has_fallback

    resp = await client.complete(system="You are the Router.", user="Ticker: SPY")

    assert resp.text == '{"score": 61}'
    assert resp.model == "claude-sonnet-5-5"
    assert resp.provider == "anthropic"
    assert resp.fallback is True
    assert primary.calls[0]["model"] == "glm-5.3", "GLM is still tried first"
    assert fallback.calls[0]["model"] == "claude-sonnet-5-5"


async def test_the_fallback_request_is_shaped_for_sonnet_5_5_not_for_glm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sonnet 5.5 rejects a non-default temperature with a 400. Sent the
    GLM request unchanged, the fallback would fail on every single call
    and the outage would look exactly as it did before."""
    client, primary, fallback = _glm_with_fallback(
        monkeypatch, primary=[_connection_error()], fallback=[_ok()]
    )
    await client.complete(
        system="You are the Router.", user="Ticker: SPY", model=Model.HAIKU, max_tokens=300
    )

    sent_to_glm, sent_to_sonnet = primary.calls[0], fallback.calls[0]
    assert sent_to_glm["temperature"] == 0.0
    assert "thinking" not in sent_to_glm
    assert "temperature" not in sent_to_sonnet
    assert sent_to_sonnet["thinking"] == {"type": "between_tools"}
    assert sent_to_sonnet["output_config"] == {"effort": "medium"}
    assert sent_to_sonnet["max_tokens"] == 300, "the caller's budget is not inflated"
    assert sent_to_sonnet["system"] == sent_to_glm["system"]
    assert sent_to_sonnet["messages"] == sent_to_glm["messages"]


async def test_without_an_anthropic_key_a_glm_failure_raises_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Anthropic key was removed on 2026-09-11 to stop the spend.
    Putting it back is the opt-in; without it nothing changes."""
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = LLM()
    assert client.has_fallback is False
    primary = _FakeClient([_connection_error()])
    monkeypatch.setattr(client, "_get_client", lambda: primary)

    with pytest.raises(anthropic.APIConnectionError):
        await client.complete(system="You are the Router.", user="Ticker: SPY")


async def test_the_fallback_can_be_switched_off_without_removing_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "0")
    client, _, fallback = _glm_with_fallback(
        monkeypatch, primary=[_connection_error()], fallback=[_ok()]
    )
    assert client.has_fallback is False
    with pytest.raises(anthropic.APIConnectionError):
        await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert fallback.calls == []


def test_anthropic_as_primary_has_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert LLM().has_fallback is False


def test_a_placeholder_anthropic_key_does_not_arm_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "replace_me")
    assert LLM().has_fallback is False


# ─────────────────────────────────────────────────────────────────────
# Which failures count as "unreachable"
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", [401, 403, 404, 429, 500, 503, 529])
async def test_a_provider_side_status_falls_back(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    client, _, fallback = _glm_with_fallback(
        monkeypatch, primary=[_status_error(code)], fallback=[_ok()]
    )
    resp = await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert resp.fallback is True
    assert len(fallback.calls) == 1
    assert fallback_status()["reason"] == f"http_{code}"


@pytest.mark.parametrize("code", [400, 413, 422])
async def test_a_request_error_is_raised_not_masked(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """A 400 is OUR request being wrong. Retrying it on a second provider
    hides the bug and pays twice for the privilege."""
    client, _, fallback = _glm_with_fallback(
        monkeypatch, primary=[_status_error(code)], fallback=[_ok()]
    )
    with pytest.raises(anthropic.APIStatusError):
        await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert fallback.calls == []
    assert fallback_status()["active"] is False


def test_a_timeout_is_named_separately_from_a_refused_connection() -> None:
    assert unreachable_reason(anthropic.APITimeoutError(request=_REQUEST)) == "timeout"
    assert unreachable_reason(_connection_error()) == "connection_error"
    assert unreachable_reason(ValueError("parse")) is None


# ─────────────────────────────────────────────────────────────────────
# The breaker
# ─────────────────────────────────────────────────────────────────────


async def test_an_outage_costs_one_failed_call_not_one_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the breaker every call waits out GLM's timeout and retries
    before falling back: 60s x 3 attempts x 5 calls a pass."""
    client, primary, fallback = _glm_with_fallback(
        monkeypatch, primary=[_connection_error()], fallback=[_ok(), _ok(), _ok()]
    )
    for _ in range(3):
        resp = await client.complete(system="You are the Router.", user="Ticker: SPY")
        assert resp.fallback is True

    assert len(primary.calls) == 1, "the dead primary was tried once"
    assert len(fallback.calls) == 3
    assert fallback_status()["servedSinceTrip"] == 3


async def test_the_breaker_is_shared_by_every_client_in_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LLM()`` is built per council pass. A breaker on the instance would
    forget the outage at the start of every pass."""
    first, _, _ = _glm_with_fallback(
        monkeypatch, primary=[_connection_error()], fallback=[_ok()]
    )
    await first.complete(system="You are the Router.", user="Ticker: SPY")

    second, primary2, fallback2 = _glm_with_fallback(
        monkeypatch, primary=[_ok()], fallback=[_ok()]
    )
    resp = await second.complete(system="You are the Router.", user="Ticker: QQQ")
    assert resp.fallback is True
    assert primary2.calls == []
    assert len(fallback2.calls) == 1


async def test_after_the_cooldown_one_probe_puts_the_desk_back_on_glm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(llm_mod, "_now", lambda: clock["t"])
    client, primary, _ = _glm_with_fallback(
        monkeypatch, primary=[_connection_error(), _ok('{"from": "glm"}')], fallback=[_ok()]
    )
    await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert fallback_status()["active"] is True

    clock["t"] += 301.0
    resp = await client.complete(system="You are the Router.", user="Ticker: SPY")

    assert resp.text == '{"from": "glm"}'
    assert resp.provider == "glm"
    assert resp.fallback is False
    assert fallback_status()["active"] is False
    assert primary.options == [{"max_retries": 0}], "the probe does not retry"


async def test_a_failed_probe_reopens_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(llm_mod, "_now", lambda: clock["t"])
    client, primary, _ = _glm_with_fallback(
        monkeypatch,
        primary=[_connection_error(), _status_error(503)],
        fallback=[_ok(), _ok(), _ok()],
    )
    await client.complete(system="You are the Router.", user="Ticker: SPY")
    clock["t"] += 301.0
    await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert len(primary.calls) == 2
    assert fallback_status()["reason"] == "http_503"

    clock["t"] += 10.0
    await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert len(primary.calls) == 2, "inside the new cooldown the primary is left alone"


async def test_the_cooldown_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(llm_mod, "_now", lambda: clock["t"])
    monkeypatch.setenv("LLM_FALLBACK_COOLDOWN_SECONDS", "30")
    client, primary, _ = _glm_with_fallback(
        monkeypatch, primary=[_connection_error(), _ok()], fallback=[_ok()]
    )
    await client.complete(system="You are the Router.", user="Ticker: SPY")
    clock["t"] += 31.0
    resp = await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert resp.provider == "glm"
    assert len(primary.calls) == 2


async def test_both_providers_down_raises_the_fallback_error_with_its_cause(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, _, _ = _glm_with_fallback(
        monkeypatch, primary=[_connection_error()], fallback=[_status_error(529)]
    )
    with (
        caplog.at_level(logging.ERROR, logger="agents.llm"),
        pytest.raises(anthropic.APIStatusError) as raised,
    ):
        await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert raised.value.status_code == 529
    assert isinstance(raised.value.__cause__, anthropic.APIConnectionError)
    assert any("also failed" in r.getMessage() for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────
# Keys never cross providers
# ─────────────────────────────────────────────────────────────────────


def test_the_fallback_client_talks_to_anthropic_with_the_anthropic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fallback-key")
    client = LLM()

    fb = client._get_fallback_client()
    assert "z.ai" not in str(fb.base_url)
    assert fb.auth_headers.get("X-Api-Key") == "sk-ant-test-fallback-key"
    assert not any("glm-test-key" in str(v) for v in fb.auth_headers.values())

    primary = client._get_client()
    assert "z.ai" in str(primary.base_url)
    assert not any("sk-ant" in str(v) for v in primary.auth_headers.values())


# ─────────────────────────────────────────────────────────────────────
# Accounting
# ─────────────────────────────────────────────────────────────────────


def test_the_default_fallback_model_is_priced_at_list() -> None:
    assert fallback_model() == FALLBACK_MODEL == "claude-sonnet-5-5"
    assert FALLBACK_MODEL in _PRICES
    cost = compute_cost_usd(
        model="claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=200_000
    )
    assert cost == pytest.approx(2.00 + 2.00)


async def test_the_ledger_bills_the_model_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fallback call recorded under the GLM id would be priced at $1.40
    in when $2.00 was billed, and the spend cap would undercount an outage."""
    client, _, _ = _glm_with_fallback(
        monkeypatch,
        primary=[_connection_error()],
        fallback=[_Message([_Block("{}")], usage=_Usage(1_000_000, 200_000))],
    )
    await client.complete(system="You are the Router on a quant desk.", user="Ticker: SPY")

    rows = await get_cost_ledger().all()
    assert len(rows) == 1
    assert rows[0].model == "claude-sonnet-5-5"
    assert rows[0].cost_usd == pytest.approx(4.00)
    assert rows[0].is_mock is False


async def test_decide_names_the_provider_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = '{"direction": "bullish", "conviction": 0.6, "confidence": 0.7, "thesis": "x"}'
    client, _, _ = _glm_with_fallback(
        monkeypatch, primary=[_connection_error()], fallback=[_ok(answer)]
    )
    d = await client.decide(system="You are an analyst.", user="SPY")
    assert d.abstained is False
    assert d.direction == "bullish"
    assert d.provider == "anthropic"
    assert d.model == "claude-sonnet-5-5"


# ─────────────────────────────────────────────────────────────────────
# Tool calling across the switch
# ─────────────────────────────────────────────────────────────────────

_TOOL = {
    "name": "get_quote",
    "description": "Latest quote.",
    "input_schema": {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    },
}


async def test_a_tool_loop_survives_glm_dying_between_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round one is GLM asking for a tool, then GLM goes away. Round two
    must reach Sonnet with the SAME history, and that history must hold
    only text and tool_use blocks: a vendor's thinking block replayed to
    the other vendor is a 400."""
    tool_turn = _Message(
        [_Block("checking"), _Block(type="tool_use", id="call_1", name="get_quote",
                                    input={"symbol": "SPY"})],
        stop_reason="tool_use",
    )
    client, _, fallback = _glm_with_fallback(
        monkeypatch,
        primary=[tool_turn, _connection_error()],
        fallback=[_ok("final answer")],
    )

    async def dispatch(call: Any) -> dict[str, Any]:
        return {"content": {"last": 518.9}}

    resp, transcript = await run_tool_loop(
        client, system="You are the Bull.", user="SPY", tools=[_TOOL], dispatch=dispatch
    )

    assert resp.text == "final answer"
    assert resp.fallback is True
    assert len(transcript) == 1
    sent = fallback.calls[0]
    assert sent["tools"] == [_TOOL]
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assert {b["type"] for b in sent["messages"][1]["content"]} == {"text", "tool_use"}
    assert sent["messages"][2]["content"][0]["tool_use_id"] == "call_1"


# ─────────────────────────────────────────────────────────────────────
# request_shape
# ─────────────────────────────────────────────────────────────────────


def test_models_that_take_a_temperature_still_get_zero() -> None:
    for model in ("glm-5.3", "glm-5.3-flash", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"):
        shape = request_shape(model)
        assert shape == {"temperature": 0.0}, model


def test_opus_4_7_is_no_longer_sent_a_temperature() -> None:
    """``Model.OPUS`` is claude-opus-4-7, which rejects a non-default
    temperature. This file sent 0.0 to every model, so
    ``OPTIONS_AGENT_MODEL=opus`` failed every Bull and Bear call."""
    assert request_shape(Model.OPUS) == {}


def test_sonnet_5_5_runs_with_thinking_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert request_shape("claude-sonnet-5-5") == {
        "thinking": {"type": "between_tools"},
        "output_config": {"effort": "medium"},
    }
    monkeypatch.setenv("LLM_EFFORT", "low")
    assert request_shape("claude-sonnet-5-5")["output_config"] == {"effort": "low"}
    # between_tools rejects anything above high; a bad value keeps the default.
    monkeypatch.setenv("LLM_EFFORT", "max")
    assert request_shape("claude-sonnet-5-5")["output_config"] == {"effort": "medium"}


def test_between_tools_is_sent_to_sonnet_5_5_only() -> None:
    """Every other model rejects ``between_tools`` outright."""
    for model in ("claude-sonnet-5", "claude-opus-5-5", "claude-haiku-5-5", "glm-5.3"):
        assert "thinking" not in request_shape(model), model


def test_a_forced_tool_choice_becomes_auto_where_forcing_is_rejected() -> None:
    forced = {"type": "tool", "name": "get_quote"}
    assert request_shape("claude-sonnet-5-5", tool_choice=forced)["tool_choice"] == {
        "type": "auto"
    }
    assert request_shape("glm-5.3", tool_choice=forced)["tool_choice"] == forced
    assert request_shape("claude-sonnet-5-5", tool_choice={"type": "none"})[
        "tool_choice"
    ] == {"type": "none"}


async def test_a_refusal_is_named_in_the_log_and_comes_back_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    @dataclass
    class _Details:
        category: str = "general_harms"

    refused = _Message([], stop_reason="refusal", stop_details=_Details())
    client, _, _ = _glm_with_fallback(
        monkeypatch, primary=[_connection_error()], fallback=[refused]
    )
    with caplog.at_level(logging.WARNING, logger="agents.llm"):
        resp = await client.complete(system="You are the Router.", user="Ticker: SPY")
    assert resp.text == ""
    assert resp.stop_reason == "refusal"
    assert any("general_harms" in r.getMessage() for r in caplog.records)
