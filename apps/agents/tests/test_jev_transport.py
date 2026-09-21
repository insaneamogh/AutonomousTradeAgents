"""Transport + `LLM.decide()` — the parts that touch the wire.

`test_jev.py` covers the request/response CONTRACT. This file covers what
happens around it: retries, redirects, credential leaks, and the abstain
path that every failure mode has to land in.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from trading_agents import jev
from trading_agents.llm import LLM, Decision, _unit


def _ok_body(direction: str = "bullish", score: float = 3) -> dict:
    dist = {d: (0.8 if d == direction else 0.1) for d in jev.DIRECTIONS}
    scores = {str(i): (0.6 if i == int(score) else 0.1) for i in range(5)}
    return {
        "answers": {
            "direction": {
                "type": "choice", "choice": direction,
                "confidence": 0.75, "probabilities": dist,
            },
            **{
                f"{d}_strength": {
                    "type": "score", "score": score if d == direction else 1,
                    "confidence": 0.7, "probabilities": scores,
                }
                for d in ("bullish", "bearish")
            },
        }
    }


@pytest.fixture
def mock_jev(monkeypatch):
    """Swap httpx's transport, keeping the real AsyncClient and the real
    request our code builds — so header and redirect behaviour are exercised
    rather than stubbed over."""
    calls: list[httpx.Request] = []

    def install(handler):
        real = httpx.AsyncClient

        # A SUBCLASS, not a factory function. The Anthropic SDK does
        # `class _DefaultAsyncHttpxClient(httpx.AsyncClient)` at import time,
        # so replacing the name with a function breaks importing anthropic at
        # all — and `LLM()` imports it on every non-mock construction.
        class _Patched(real):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **kw):
                kw["transport"] = httpx.MockTransport(
                    lambda req: (calls.append(req), handler(req, len(calls)))[1]
                )
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", _Patched)
        return calls

    return install


async def test_happy_path_returns_the_decoded_body(mock_jev):
    mock_jev(lambda req, n: httpx.Response(200, json=_ok_body()))
    body = await jev.call(jev.build_request(system="s", user="u"), api_key="k")
    assert jev.parse_response(body).direction == "bullish"


async def test_the_api_key_travels_as_a_bearer_token(mock_jev):
    calls = mock_jev(lambda req, n: httpx.Response(200, json=_ok_body()))
    await jev.call(jev.build_request(system="s", user="u"), api_key="secret-key")
    assert calls[0].headers["Authorization"] == "Bearer secret-key"


async def test_a_redirect_is_NOT_followed(mock_jev):
    """httpx forwards Authorization across redirects. An upstream 302 to
    another host would hand our API key to that host, so the client must
    refuse to follow rather than trust the redirect target."""
    calls = mock_jev(
        lambda req, n: httpx.Response(302, headers={"Location": "https://evil.test/x"})
    )
    with pytest.raises(jev.JevTransportError):
        await jev.call(jev.build_request(system="s", user="u"), api_key="secret-key")
    assert len(calls) == 1, "followed the redirect"
    assert all("evil.test" not in str(c.url) for c in calls)


async def test_a_429_is_retried_once_then_succeeds(mock_jev):
    calls = mock_jev(
        lambda req, n: httpx.Response(200, json=_ok_body())
        if n > 1
        else httpx.Response(429, headers={"Retry-After": "0"})
    )
    body = await jev.call(jev.build_request(system="s", user="u"), api_key="k")
    assert len(calls) == 2
    assert jev.parse_response(body).direction == "bullish"


async def test_retries_are_not_infinite(mock_jev):
    calls = mock_jev(lambda req, n: httpx.Response(503, headers={"Retry-After": "0"}))
    with pytest.raises(jev.JevTransportError):
        await jev.call(jev.build_request(system="s", user="u"), api_key="k")
    assert len(calls) == 2


async def test_a_400_is_NOT_retried(mock_jev):
    """A bad request is bad the second time too. Retrying a 4xx burns the
    latency budget of a live council pass for nothing."""
    calls = mock_jev(lambda req, n: httpx.Response(400, text="bad question set"))
    with pytest.raises(jev.JevTransportError):
        await jev.call(jev.build_request(system="s", user="u"), api_key="k")
    assert len(calls) == 1


async def test_an_echoed_api_key_is_redacted_from_the_error(mock_jev):
    """Upstreams do echo the Authorization header back in error bodies. That
    error text ends up in logs and exception messages."""
    mock_jev(lambda req, n: httpx.Response(401, text="bad token: Bearer sk-live-xyz"))
    with pytest.raises(jev.JevTransportError) as ei:
        await jev.call(jev.build_request(system="s", user="u"), api_key="sk-live-xyz")
    assert "sk-live-xyz" not in str(ei.value)
    assert "[REDACTED]" in str(ei.value)


async def test_non_json_is_a_transport_error_not_a_crash(mock_jev):
    mock_jev(lambda req, n: httpx.Response(200, text="<html>502 gateway</html>"))
    with pytest.raises(jev.JevTransportError):
        await jev.call(jev.build_request(system="s", user="u"), api_key="k")


async def test_an_empty_key_never_reaches_the_network(mock_jev):
    calls = mock_jev(lambda req, n: httpx.Response(200, json=_ok_body()))
    with pytest.raises(jev.JevTransportError):
        await jev.call(jev.build_request(system="s", user="u"), api_key="   ")
    assert calls == []


def test_retry_after_floors_at_one_second():
    """A past HTTP-date or a malformed header must not turn the retry into a
    hot loop against a rate limiter."""
    assert jev.retry_delay_seconds(None) == 1.0
    assert jev.retry_delay_seconds("garbage") == 1.0
    assert jev.retry_delay_seconds("0") == 1.0
    assert jev.retry_delay_seconds("Wed, 01 Jan 2020 00:00:00 GMT") == 1.0
    assert jev.retry_delay_seconds("30") == 30.0


# ── LLM.decide() ────────────────────────────────────────────────────

async def test_decide_on_jev_maps_score_to_conviction(monkeypatch, mock_jev):
    monkeypatch.setenv("LLM_PROVIDER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    mock_jev(lambda req, n: httpx.Response(200, json=_ok_body("bearish", 4)))
    d = await LLM().decide(system="s", user="u")
    assert (d.direction, d.conviction, d.abstained) == ("bearish", 1.0, False)
    assert d.model == "jev-1.13.0"


async def test_neutral_carries_zero_conviction(monkeypatch, mock_jev):
    """A neutral read sized like a view is how 'no opinion' becomes a trade."""
    monkeypatch.setenv("LLM_PROVIDER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    mock_jev(lambda req, n: httpx.Response(200, json=_ok_body("neutral", 4)))
    assert (await LLM().decide(system="s", user="u")).conviction == 0.0


async def test_a_transport_failure_abstains_rather_than_going_neutral(
    monkeypatch, mock_jev
):
    """The failure contract: a model we could not reach is NOT a model that
    said 'no view'. Collapsing them hides an outage as a quiet HOLD."""
    monkeypatch.setenv("LLM_PROVIDER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    mock_jev(lambda req, n: httpx.Response(500, headers={"Retry-After": "0"}))
    d = await LLM().decide(system="s", user="u")
    assert d.abstained is True and d.direction == "neutral"


async def test_a_malformed_answer_abstains_and_is_never_coerced(
    monkeypatch, mock_jev
):
    monkeypatch.setenv("LLM_PROVIDER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    mock_jev(
        lambda req, n: httpx.Response(
            200, json={"answers": {"direction": {"type": "choice", "choice": "up"}}}
        )
    )
    assert (await LLM().decide(system="s", user="u")).abstained is True


async def test_decide_on_a_prose_provider_parses_the_same_shape(monkeypatch):
    """Jev and Sonnet must produce the same `Decision`, or an A/B between
    them compares two different questions."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    llm = LLM()

    async def fake(**kw):
        from trading_agents.llm import LLMResponse

        return LLMResponse(
            text='{"direction":"bullish","conviction":0.75,'
            '"confidence":0.6,"thesis":"Trend intact."}',
            model="claude-sonnet-4-6",
        )

    monkeypatch.setattr(llm, "complete", fake)
    d = await llm.decide(system="s", user="u")
    assert (d.direction, d.conviction, d.thesis) == ("bullish", 0.75, "Trend intact.")
    assert d.abstained is False


async def test_unparseable_prose_abstains(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    llm = LLM()

    async def fake(**kw):
        from trading_agents.llm import LLMResponse

        return LLMResponse(text="I'd rather not say.", model="claude-sonnet-4-6")

    monkeypatch.setattr(llm, "complete", fake)
    assert (await llm.decide(system="s", user="u")).abstained is True


async def test_mock_mode_abstains_instead_of_inventing_a_view(monkeypatch):
    """MOCK must never look like a real judgement: a canned direction with a
    real-looking conviction is exactly what reaches an approval inbox."""
    for k in ("ANTHROPIC_API_KEY", "GLM_API_KEY", "ZAI_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    d = await LLM().decide(system="s", user="u")
    assert d.abstained is True and d.conviction == 0.0


def test_unit_clamps_and_rescales():
    assert _unit(0.5) == 0.5
    assert _unit(85) == 0.85, "a model answering 85 for a 0-1 field means 0.85"
    assert _unit(-3) == 0.0
    assert _unit(1000) == 1.0
    assert _unit("nonsense") == 0.0
    assert _unit(float("nan")) == 0.0


def test_decision_is_immutable():
    """It is an audit record of what the model said. A later stage adjusting
    conviction in place would make the ledger a lie."""
    d = Decision(
        direction="bullish", conviction=0.5, confidence=0.5,
        thesis="", provider="jev", model="jev-1.13.0",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.conviction = 0.9  # type: ignore[misc]


def test_a_neutral_decision_cannot_carry_conviction():
    """The invariant, tested at the ONE place that now enforces it.

    Every provider path funnels through this constructor, so a future branch
    that forgets to zero a neutral cannot reintroduce the bug.
    """
    d = Decision(
        direction="neutral", conviction=0.9, confidence=0.8,
        thesis="", provider="test", model="test",
    )
    assert d.conviction == 0.0
    assert Decision(
        direction="bullish", conviction=0.9, confidence=0.8,
        thesis="", provider="test", model="test",
    ).conviction == 0.9, "a directional read must keep its conviction"
