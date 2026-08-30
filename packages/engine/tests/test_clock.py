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

from engine.features import alpaca_cli, clock
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


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
def test_use_alpaca_cli_recognises_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("USE_ALPACA_CLI", value)
    assert use_alpaca_cli() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "nonsense"])
def test_use_alpaca_cli_defaults_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("USE_ALPACA_CLI", value)
    assert use_alpaca_cli() is False


def test_use_alpaca_cli_defaults_off_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_ALPACA_CLI", raising=False)
    assert use_alpaca_cli() is False


# ─────────────────────────────────────────────────────────────────────
# resolve_market_clock — fallback ordering
# ─────────────────────────────────────────────────────────────────────


async def test_resolve_market_clock_never_touches_cli_when_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is the only thing D.3 adds — off means the CLI step must
    not even be invoked, not merely "invoked but ignored"."""
    monkeypatch.delenv("USE_ALPACA_CLI", raising=False)
    monkeypatch.setattr(alpaca_cli, "cli_clock", _never_called)
    fake = FakeClock(MarketClock(is_open=True, source="alpaca"))

    result = await resolve_market_clock(at=AT, alpaca=fake)

    assert result.source == "alpaca"
    assert fake.calls == 1


async def test_resolve_market_clock_falls_back_to_local_calendar_when_flag_is_off_and_no_alpaca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("USE_ALPACA_CLI", raising=False)
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


async def test_resolve_market_clock_falls_back_to_rest_when_cli_enabled_but_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_ALPACA_CLI", "1")

    async def fake_cli_clock(**_: object) -> MarketClock | None:
        return None

    monkeypatch.setattr(alpaca_cli, "cli_clock", fake_cli_clock)
    fake_rest = FakeClock(MarketClock(is_open=True, source="alpaca"))

    result = await resolve_market_clock(at=AT, alpaca=fake_rest)

    assert result.source == "alpaca"
    assert fake_rest.calls == 1


async def test_resolve_market_clock_falls_back_to_local_calendar_when_cli_and_rest_both_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_ALPACA_CLI", "1")

    async def fake_cli_clock(**_: object) -> MarketClock | None:
        return None

    monkeypatch.setattr(alpaca_cli, "cli_clock", fake_cli_clock)

    result = await resolve_market_clock(at=AT, alpaca=None)

    assert result.source == "local_calendar"
    assert result.is_open == clock.is_us_market_open(AT)


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


async def test_resolved_clock_from_env_with_no_keys_falls_back_to_local_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    monkeypatch.delenv("USE_ALPACA_CLI", raising=False)

    provider = resolved_clock_from_env()
    result = await provider.now(at=AT)

    assert result.source == "local_calendar"
