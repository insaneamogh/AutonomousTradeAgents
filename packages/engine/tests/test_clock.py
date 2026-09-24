"""``engine.features.clock`` — ``resolve_market_clock``'s CLI-first fallback
chain (``docs/PLAN_ALPACA_MCP.md`` D.3).

The chain is CLI (only when ``USE_ALPACA_CLI=1``) -> ``alpaca`` (REST) ->
local calendar. The whole point of D.3 is that flipping the flag is the
ONLY thing that changes behaviour — every test below that leaves
``USE_ALPACA_CLI`` unset asserts the CLI step is never even reached.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.features import alpaca_cli
from engine.features.clock import (
    ClockProvider,
    MarketClock,
    ResolvingClock,
    resolve_market_clock,
    resolved_clock_from_env,
    use_alpaca_cli,
)

AT = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)  # Mon, mid-session


class FakeClock:
    """A minimal ``ClockProvider`` double — same shape as
    ``test_scanner_engine.py``'s ``FakeClock``, duplicated here rather than
    imported so this test file has no dependency on scanner test internals."""

    name = "fake-clock"

    def __init__(self, value: MarketClock) -> None:
        self.value = value
        self.calls = 0

    async def now(self, *, at: datetime | None = None) -> MarketClock:
        self.calls += 1
        return self.value


async def _never_called(**_: object) -> MarketClock | None:
    raise AssertionError("cli_clock must not be called when USE_ALPACA_CLI is off")


# ─────────────────────────────────────────────────────────────────────
# use_alpaca_cli() — flag parsing
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "OFF"])
def test_use_alpaca_cli_is_disabled_only_by_an_explicit_falsy_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("USE_ALPACA_CLI", value)
    assert use_alpaca_cli() is False


# ─────────────────────────────────────────────────────────────────────
# resolve_market_clock — fallback ordering
# ─────────────────────────────────────────────────────────────────────


async def test_resolve_market_clock_falls_back_to_local_calendar_when_flag_is_off_and_no_alpaca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_ALPACA_CLI", "0")
    monkeypatch.setattr(alpaca_cli, "cli_clock", _never_called)

    result = await resolve_market_clock(at=AT, alpaca=None)

    assert result.source == "local_calendar"


async def test_resolve_market_clock_uses_the_cli_result_when_enabled_and_it_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the CLI answers, its result wins outright — the REST clock must
    not even be asked (the whole point of putting the CLI first)."""
    monkeypatch.setenv("USE_ALPACA_CLI", "1")
    cli_result = MarketClock(is_open=True, source="alpaca_cli")

    async def fake_cli_clock(**_: object) -> MarketClock | None:
        return cli_result

    monkeypatch.setattr(alpaca_cli, "cli_clock", fake_cli_clock)
    fake_rest = FakeClock(MarketClock(is_open=False, source="alpaca"))

    result = await resolve_market_clock(at=AT, alpaca=fake_rest)

    assert result is cli_result
    assert result.source == "alpaca_cli"
    assert fake_rest.calls == 0, "the REST clock must not be consulted when the CLI answers"


# ─────────────────────────────────────────────────────────────────────
# ResolvingClock / resolved_clock_from_env — the Scanner-facing wiring
# ─────────────────────────────────────────────────────────────────────


def test_resolving_clock_satisfies_the_clock_provider_protocol() -> None:
    rc = ResolvingClock(alpaca=None)
    assert isinstance(rc, ClockProvider)


async def test_resolving_clock_delegates_to_resolve_market_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("USE_ALPACA_CLI", raising=False)
    fake_rest = FakeClock(MarketClock(is_open=True, source="alpaca"))
    rc = ResolvingClock(alpaca=fake_rest)

    result = await rc.now(at=AT)

    assert result.source == "alpaca"
    assert fake_rest.calls == 1


def test_resolved_clock_from_env_wraps_clock_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no Alpaca keys configured, ``clock_from_env()`` is None — the
    wrapper must still hand back a usable ``ClockProvider``, not None,
    exactly mirroring ``clock_from_env()``'s own no-keys contract at one
    layer up."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)

    provider = resolved_clock_from_env()

    assert isinstance(provider, ResolvingClock)
    assert provider.alpaca is None
