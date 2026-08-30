"""``alpaca`` CLI subprocess wrapper — the hackathon eligibility artifact.

The rule is: *"projects must utilize either Alpaca's own MCP server or its
CLI tools."* ``github.com/alpacahq/cli`` ships a prebuilt ``alpaca`` binary
explicitly built for *"long-running agent sessions, cron jobs and CI"*
(verified README quote — D.0, ``fable5findings.md``) — exactly the shape of
``apps/api/app/services/council/scheduler.py``'s scan loop.

This module wraps exactly one subcommand: ``alpaca clock``. Two of the
plan's three named traps were verified wrong at D.0 and do NOT apply here:

  - The subcommand is ``alpaca clock`` — no ``get`` suffix. (Verified twice,
    independently, from the CLI's own README.)
  - No env var remapping is needed. The README documents reading
    ``ALPACA_API_KEY``/``ALPACA_SECRET_KEY`` directly — the same names
    ``engine.features.clock`` already uses — so the subprocess ``env=`` dict
    is a plain passthrough, no ``APCA_``-prefixed mapping.
  - JSON is already the CLI's default stdout format (``--csv`` is what opts
    OUT of it), so no output-format flag is passed either.

Safety rules — each has a named test in ``tests/test_alpaca_cli.py`` because
each has already bitten someone on this class of code:

  - ``asyncio.create_subprocess_exec`` with an argv list. **Never**
    ``shell=True``, **never** string-interpolation into the command. A
    subprocess launched from inside the FastAPI process is a new attack
    surface; the argv boundary is the whole defense.
  - ``asyncio.wait_for(proc.communicate(), timeout)`` **and** ``proc.kill()``
    in the timeout except-path. Skipping the kill leaks a zombie subprocess
    every tick on a loop that runs every 30 seconds
    (``docs/PLAN_ALPACA_MCP.md`` D.3).
  - ``cli_clock()`` returns ``None`` on ANY failure — missing binary,
    non-zero exit, timeout, unparseable JSON. It never raises. A dev laptop
    with no ``alpaca`` binary installed must hit this path silently and
    correctly; it is the first link in ``clock.resolve_market_clock``'s
    fallback chain, not a required dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime

from engine.features.clock import MarketClock

logger = logging.getLogger("engine.features.alpaca_cli")

CLI_BINARY = "alpaca"
CLI_CLOCK_SOURCE = "alpaca_cli"

DEFAULT_TIMEOUT_SECONDS = 5.0


# `timeout` is a deliberate, plan-specified parameter
# (docs/PLAN_ALPACA_MCP.md D.3's own signature), not an oversight: the caller
# (`resolve_market_clock`) does not wrap this in `asyncio.timeout()`, so the
# bound has to live here to cap the subprocess wait. Ruff's suggested
# alternative would move the timeout to the call site instead of the callee —
# a reasonable style in general, but it would leave `cli_clock` itself
# unbounded if a future caller forgets to wrap it.
async def cli_clock(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109
) -> MarketClock | None:
    """``alpaca clock`` via subprocess. Returns ``None`` on ANY failure.

    Never raises: a missing binary, a non-zero exit, a timeout, or
    unparseable JSON all degrade to ``None`` so the caller
    (``clock.resolve_market_clock``) can fall through to the next link in
    the chain (Alpaca's REST ``/v2/clock``, then the local calendar).
    """
    try:
        return await _cli_clock_or_raise(timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # last-resort net — this function must never raise
        logger.warning(
            "alpaca_cli: unexpected failure calling `%s clock` (%s) — falling back",
            CLI_BINARY,
            exc,
        )
        return None


async def _cli_clock_or_raise(cli_timeout_seconds: float) -> MarketClock | None:
    """The unguarded body of ``cli_clock`` — split out only so the one
    top-level ``except Exception`` above stays a one-line safety net rather
    than wrapping fifty lines of unrelated logic."""
    try:
        proc = await asyncio.create_subprocess_exec(
            CLI_BINARY,
            "clock",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_cli_env(),
        )
    except OSError as exc:
        # FileNotFoundError (binary not on PATH) is the expected dev-laptop
        # case — OSError is the broader net (permissions, etc.) but every
        # member of it means "could not even launch", same fallback either way.
        logger.info(
            "alpaca_cli: `%s clock` unavailable (%s) — falling back to "
            "REST/local calendar. Expected when the CLI isn't installed.",
            CLI_BINARY,
            exc,
        )
        return None

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cli_timeout_seconds)
    except TimeoutError:
        # The single most important line in this module: without the kill,
        # a scan loop that runs every 30s leaks one zombie subprocess per
        # tick forever. `wait()` afterward actually reaps it rather than
        # just signalling it.
        proc.kill()
        await proc.wait()
        logger.warning(
            "alpaca_cli: `%s clock` timed out after %.1fs — killed the process, falling back",
            CLI_BINARY,
            cli_timeout_seconds,
        )
        return None

    if proc.returncode != 0:
        logger.info(
            "alpaca_cli: `%s clock` exited %s (%s) — falling back",
            CLI_BINARY,
            proc.returncode,
            stderr.decode("utf-8", "replace").strip()[:200],
        )
        return None

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.warning(
            "alpaca_cli: unparseable JSON from `%s clock` (%s) — falling back",
            CLI_BINARY,
            exc,
        )
        return None

    clock = _parse_cli_clock(payload)
    if clock is None:
        logger.warning(
            "alpaca_cli: `%s clock` JSON missing the expected fields — falling back",
            CLI_BINARY,
        )
    return clock


def _cli_env() -> dict[str, str]:
    """The subprocess environment — passthrough, no name remapping.

    D.0 verified the CLI's own README documents reading ``ALPACA_API_KEY``/
    ``ALPACA_SECRET_KEY`` directly, the exact names ``engine.features.clock``
    already requires to build an ``AlpacaClock`` — so there is no
    ``APCA_``-prefixed name to map, unlike what the plan predicted before
    verifying. Built as an explicit dict scoped to this one subprocess call
    (never assigned into ``os.environ`` / no new globals) so the boundary
    stays visible and auditable — this is a subprocess launched from inside
    the FastAPI process, a new attack surface worth being deliberate about.
    """
    return dict(os.environ)


def _parse_cli_clock(payload: object) -> MarketClock | None:
    """``alpaca clock``'s JSON -> ``MarketClock``, or ``None`` if it doesn't
    look like one.

    The CLI wraps the same Trading API ``/v2/clock`` call
    ``engine.features.clock.AlpacaClock`` hits directly, and its README
    states JSON-out mirrors the API by default — so this expects the same
    ``is_open``/``next_open``/``next_close`` shape as the REST payload.
    **Not verified against a real ``alpaca`` binary** (D.0 covered the
    flag/env surface, not the literal JSON shape) — treated here as an
    inference: a payload missing the ``is_open`` key is treated as a parse
    failure (``None``) rather than silently defaulting to "closed" under a
    ``source="alpaca_cli"`` label that would misrepresent a malformed/
    unexpected response as a real answer.
    """
    if not isinstance(payload, dict) or "is_open" not in payload:
        return None
    return MarketClock(
        is_open=bool(payload.get("is_open", False)),
        next_open=_dt(payload.get("next_open")),
        next_close=_dt(payload.get("next_close")),
        source=CLI_CLOCK_SOURCE,
    )


def _dt(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).astimezone(UTC)
    except ValueError:
        return None
