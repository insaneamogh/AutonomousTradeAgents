"""``engine.features.alpaca_cli`` — the ``alpaca clock`` subprocess wrapper.

Each test exists because the failure mode it covers has a named consequence
in production, per ``docs/PLAN_ALPACA_MCP.md`` D.3:

  - A missing binary must degrade silently (every dev laptop, day one).
  - A hung process must be killed, not leaked — this loop runs every 30s.
  - Bad JSON (missing binary version, a broken release, an error payload)
    must not be misreported as a real ``source="alpaca_cli"`` answer.

None of these call the real ``alpaca`` binary — ``asyncio.create_subprocess_exec``
is monkeypatched with a fake process double so every test is deterministic and
network-free, matching this test suite's existing convention (see
``test_features_macro.py``'s ``_StubClient`` for ``httpx.AsyncClient``).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from engine.features import alpaca_cli
from engine.features.alpaca_cli import cli_clock
from engine.features.clock import MarketClock


class _FakeProcess:
    """Stands in for ``asyncio.subprocess.Process``.

    ``hang=True`` makes ``communicate()`` never return within any realistic
    test timeout, so ``asyncio.wait_for`` is the thing that actually cuts it
    off — exercising the real timeout path, not a simulated one.
    """

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, outcome: _FakeProcess | Exception) -> None:
    """Replace ``asyncio.create_subprocess_exec`` as seen from inside
    ``alpaca_cli`` — patched on the module object it was imported onto
    (``alpaca_cli.asyncio``), the same pattern ``test_features_macro.py``
    uses for ``httpx.AsyncClient``."""

    async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _FakeProcess:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(alpaca_cli.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


# ─────────────────────────────────────────────────────────────────────
# Missing binary
# ─────────────────────────────────────────────────────────────────────


async def test_clock_falls_back_when_the_cli_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FileNotFoundError`` from ``create_subprocess_exec`` (no ``alpaca`` on
    PATH — every dev laptop, day one) must be swallowed, not propagated."""
    _patch_subprocess(monkeypatch, FileNotFoundError("no such file or directory: 'alpaca'"))

    result = await cli_clock()

    assert result is None


async def test_cli_clock_returns_none_when_binary_is_genuinely_absent() -> None:
    """No mocking at all: this sandbox has no ``alpaca`` binary installed
    (verified: ``which alpaca`` fails), so this exercises the real
    ``FileNotFoundError`` path end to end, not a simulated one."""
    result = await cli_clock(timeout=2.0)

    assert result is None


# ─────────────────────────────────────────────────────────────────────
# Timeout — the single most important behaviour in this module
# ─────────────────────────────────────────────────────────────────────


async def test_cli_clock_returns_none_on_timeout_and_kills_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung ``alpaca clock`` must be killed AND reaped — skip the kill and
    a 30s scan loop leaks one zombie subprocess per tick, forever."""
    proc = _FakeProcess(hang=True)
    _patch_subprocess(monkeypatch, proc)

    result = await cli_clock(timeout=0.05)

    assert result is None
    assert proc.killed is True, "proc.kill() must be called on timeout"
    assert proc.waited is True, "proc.wait() must be awaited after kill() to reap the child"


# ─────────────────────────────────────────────────────────────────────
# Non-zero exit
# ─────────────────────────────────────────────────────────────────────


async def test_cli_clock_returns_none_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout=b"", stderr=b"error: not authenticated", returncode=1)
    _patch_subprocess(monkeypatch, proc)

    result = await cli_clock()

    assert result is None


# ─────────────────────────────────────────────────────────────────────
# Unparseable / unexpected JSON
# ─────────────────────────────────────────────────────────────────────


async def test_cli_clock_returns_none_on_unparseable_json(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout=b"not json at all {{{", returncode=0)
    _patch_subprocess(monkeypatch, proc)

    result = await cli_clock()

    assert result is None


async def test_cli_clock_returns_none_when_json_is_missing_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JSON that doesn't look like a clock response (e.g. an error
    object emitted with exit code 0) must not be silently reinterpreted as
    ``is_open=False`` under a real-looking ``source="alpaca_cli"`` label —
    that would misrepresent a broken response as a genuine answer."""
    proc = _FakeProcess(stdout=json.dumps({"error": "not authenticated"}).encode(), returncode=0)
    _patch_subprocess(monkeypatch, proc)

    result = await cli_clock()

    assert result is None


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────


async def test_cli_clock_parses_a_successful_response(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "is_open": True,
        "next_open": "2026-08-31T13:30:00Z",
        "next_close": "2026-08-30T20:00:00Z",
    }
    proc = _FakeProcess(stdout=json.dumps(payload).encode(), returncode=0)
    _patch_subprocess(monkeypatch, proc)

    result = await cli_clock()

    assert result == MarketClock(
        is_open=True,
        next_open=datetime(2026, 8, 31, 13, 30, tzinfo=UTC),
        next_close=datetime(2026, 8, 30, 20, 0, tzinfo=UTC),
        source="alpaca_cli",
    )


async def test_cli_clock_reports_closed_with_no_next_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProcess(stdout=json.dumps({"is_open": False}).encode(), returncode=0)
    _patch_subprocess(monkeypatch, proc)

    result = await cli_clock()

    assert result == MarketClock(is_open=False, source="alpaca_cli")


# ─────────────────────────────────────────────────────────────────────
# Safety-net: unexpected exceptions from the subprocess call never escape
# ─────────────────────────────────────────────────────────────────────


async def test_cli_clock_swallows_an_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, RuntimeError("something the module didn't anticipate"))

    result = await cli_clock()

    assert result is None


async def test_cli_clock_never_raises_cancelled_error_as_a_plain_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.CancelledError`` must propagate — it is not a failure mode
    to swallow, it means the caller (or the test runner) is shutting down."""

    async def _cancel_immediately(*_args: object, **_kwargs: object) -> _FakeProcess:
        raise asyncio.CancelledError()

    monkeypatch.setattr(alpaca_cli.asyncio, "create_subprocess_exec", _cancel_immediately)

    with pytest.raises(asyncio.CancelledError):
        await cli_clock()


# ─────────────────────────────────────────────────────────────────────
# Env passthrough — no APCA_ remapping (D.0 verified it isn't needed)
# ─────────────────────────────────────────────────────────────────────


def test_cli_env_passes_through_alpaca_credentials_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-456")
    # Guard against ambient environment state — the assertion below is about
    # what _cli_env() does NOT add, not about what the host happens to have.
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    env = alpaca_cli._cli_env()

    assert env["ALPACA_API_KEY"] == "test-key-123"
    assert env["ALPACA_SECRET_KEY"] == "test-secret-456"
    assert "APCA_API_KEY_ID" not in env
    assert "APCA_API_SECRET_KEY" not in env
