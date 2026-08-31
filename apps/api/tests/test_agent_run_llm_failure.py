"""An upstream Anthropic auth failure mid-council-pass must degrade to a
clean, fast, NAMED "failed" run -- never a hang, never an opaque 500, and
never indistinguishable from "the server didn't respond" on the mobile
client.

This is the live scenario docs/PLAN_NEXT.md Section 0.4 measured directly
against Railway on 2026-08-31 (a present but INVALID ANTHROPIC_API_KEY --
`AGENTS_REQUIRE_REAL_LLM=1` means there is no mock fallback, so a pass that
reaches the router simply raises). Whether that specific key ever gets
rotated is out of this test's scope entirely -- what matters is that ANY
upstream LLM auth failure, now or with a future key, produces a clean
failure a mobile client can render, not a stuck spinner or a transport
error that reads as "check your connection".

`AgentRunRegistry._drive` (apps/api/app/services/council/agent_runs.py)
already wraps the whole background run in a bare `except Exception` and
records `rec.error = str(exc)` -- by inspection that looks like it already
does the right thing. This test proves it end to end through the REAL HTTP
surface (POST /agent/run/start -> GET /agent/run/{id}/progress) with a REAL
`anthropic.AuthenticationError` (constructed the same way the SDK builds
one internally off a 401 response), rather than trusting the inspection.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app
from app.services.council.agent_runs import get_run_registry
from trading_agents import graph as graph_mod
from trading_agents.state import CouncilState


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _bearer_header(client: TestClient, email: str) -> dict[str, str]:
    """Real login flow, not the dev bypass -- POST /agent/run/start requires
    `require_real_auth` (it spends real LLM budget and writes a decision
    row), which refuses the bypass fixture user just like /agent/run does.
    See test_agent_symbol_validation.py's identical helper."""
    challenge = client.post("/api/v1/auth/request-login", json={"email": email}).json()
    verified = client.post(
        "/api/v1/auth/verify", json={"email": email, "token": challenge["devToken"]}
    ).json()
    return {"Authorization": f"Bearer {verified['accessToken']}"}


async def _force_strategy_fit_pass(state: CouncilState) -> CouncilState:
    """Stand in for the real deterministic gate.

    Mirrors test_council_mock.py's `test_drafter_skipped_when_fit_holds`
    (the same file forces a HOLD the same way) -- except this forces the
    OPPOSITE outcome. This test's only interest is what happens AFTER a
    strategy is selected and the pass reaches the LLM; whether a given
    symbol's synthetic/CI feature data happens to clear strategy_fit's own
    floor is a different, already-covered concern (test_council_mock.py),
    and hardcoding a real ticker's synthetic-hash outcome here would couple
    this test to that hash's implementation for no reason.
    """
    return {
        **state,
        "selected_strategy": "momentum",
        "selected_direction": "long",
        "selector_confidence": 0.7,
        "selector_rationale": "STUB-PASS: forced a strategy fit from a test.",
        "strategy_fit": {
            "allow_shorts": False,
            "winner": {"strategy_id": "momentum", "direction": "long", "score": 0.7},
            "ranked": [],
            "priors_applied": {},
            "usable_features": True,
        },
    }


def _auth_error() -> anthropic.AuthenticationError:
    """A REAL `anthropic.AuthenticationError`, built the same way the SDK
    constructs one internally off a 401 response -- so `str(exc)` is
    exactly the shape production would log and hand back, not a
    hand-wavy stand-in exception."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
    response = httpx.Response(401, request=request, json=body)
    return anthropic.AuthenticationError(
        "Error code: 401 - invalid x-api-key", response=response, body=body
    )


class _RaisingMessages:
    """Stands in for the real SDK's `client.messages`. `create()` raises
    immediately -- no network, no delay -- matching how a 401 actually
    behaves: the Anthropic SDK does not retry auth errors (they aren't
    transient), so this never touches `max_retries` or the client's
    60s `LLM_TIMEOUT_SECONDS`."""

    async def create(self, **kwargs: object) -> None:
        raise _auth_error()


class _FakeAsyncAnthropic:
    """Stands in for `anthropic.AsyncAnthropic` -- constructed the exact
    same way `LLM._get_client()` constructs the real one (same kwargs
    shape), but every call fails instead of reaching the network."""

    def __init__(self, **kwargs: object) -> None:
        self.messages = _RaisingMessages()


def test_dead_llm_key_degrades_to_a_clean_failed_run_not_a_hang(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full path a mobile client actually sees when the deployed
    ANTHROPIC_API_KEY is present but invalid:

      1. POST /agent/run/start returns 202 with a runId FAST. The
         background-task pattern in agent_runs.py (`asyncio.create_task`,
         never awaited by the request handler) means this can never hang
         or 500 because of what the council pass goes on to do --
         verified here by timing it, not just reading the source.
      2. GET /agent/run/{id}/progress converges on status="failed" with a
         real, legible `error` string -- fast, since an auth error is not
         retried. Never stuck at "running" forever, never a bare 500, and
         never silently downgraded to a fake HOLD.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-not-a-real-key-000000000000")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)
    monkeypatch.setattr(graph_mod, "strategy_fit_node", _force_strategy_fit_pass)

    headers = _bearer_header(client, "llm-auth-failure-test@example.com")

    t0 = time.monotonic()
    start_resp = client.post(
        "/api/v1/agent/run/start",
        json={"symbol": "AAPL", "horizon": "short"},
        headers=headers,
    )
    start_elapsed = time.monotonic() - t0

    assert start_resp.status_code == 202, start_resp.text
    assert start_elapsed < 2.0, (
        f"POST /agent/run/start took {start_elapsed:.2f}s -- it must return "
        "near-instantly regardless of the LLM outcome, since the council "
        "pass runs in a background asyncio task the handler never awaits"
    )
    run_id = start_resp.json()["runId"]

    # Poll like the mobile client does (600ms interval, see useCouncilRun.ts)
    # but capped well under what an actual hang would need to time out on
    # its own -- if this loop exhausts, the bug is real, not a fluke.
    deadline = time.monotonic() + 10.0
    progress: dict = {}
    while time.monotonic() < deadline:
        progress = client.get(f"/api/v1/agent/run/{run_id}/progress", headers=headers).json()
        if progress["status"] != "running":
            break
        time.sleep(0.1)

    assert progress["status"] == "failed", (
        f"expected a clean 'failed' status, got {progress!r} -- if this is "
        "still 'running' the auth failure is hanging the background task "
        "instead of degrading cleanly (exactly the failure mode that would "
        "look like \"the server didn't respond\" to a mobile client)"
    )
    assert progress["result"] is None, "a failed run must not also carry a stale/partial result"
    assert progress["error"], "a failed run must carry a real reason, not an empty string"
    assert "401" in progress["error"] or "authentication" in progress["error"].lower(), (
        f"expected the real auth-error message to survive to the client, got {progress['error']!r}"
    )

    # Cleanup: the registry is process-local and would otherwise leave this
    # run parked as the user's "active" run for its 15-minute TTL, which
    # could shadow a real run in a later test using the same email.
    get_run_registry()._runs.pop(run_id, None)
