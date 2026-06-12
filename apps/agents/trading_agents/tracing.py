"""Langfuse observability for the council — per-agent, fail-or-succeed.

One Langfuse *trace* per council run; one *generation* per agent node
(router / technical / fundamental / macro / selector / drafter / reflection),
each carrying the prompt, the parsed output, token usage, cost, latency, and
a level that says whether the agent succeeded, ran degraded (parse retry),
or failed (unusable output / API error).

Design rules (match the cost-ledger's "telemetry is best-effort" stance):
  - Env-gated: needs LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY. Absent → the
    client is never constructed and every helper here is a hard no-op. The
    council runs byte-identically with tracing off.
  - Never raises into the council. Every Langfuse call is wrapped; a
    tracing failure degrades to silence, never to a broken trade decision.
  - SDK version: built against langfuse 4.x
    (``start_as_current_observation(as_type=...)`` + ``.update()``). A
    version/API mismatch is caught and disables tracing rather than crashing.

The SDK flushes on a background thread, so the long-lived API needs no
explicit flush; short-lived processes (the daily cron) call ``flush()`` in
a finally so spans export before exit.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger("agents.tracing")

_client: Any = None
_resolved = False


def _resolve_client() -> Any:
    """Construct the Langfuse client once, or None when disabled."""
    global _client, _resolved
    if _resolved:
        return _client
    _resolved = True

    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public or not secret:
        logger.info("Langfuse disabled (no LANGFUSE_PUBLIC_KEY/SECRET_KEY) — tracing is a no-op")
        _client = None
        return None

    try:
        from langfuse import Langfuse

        host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").strip()
        _client = Langfuse(public_key=public, secret_key=secret, host=host)
        logger.info("Langfuse tracing ENABLED (host=%s)", host)
    except Exception:  # noqa: BLE001 — any init failure → tracing off, council unaffected
        logger.exception("Langfuse init failed — tracing disabled")
        _client = None
    return _client


def tracing_enabled() -> bool:
    return _resolve_client() is not None


def reset_for_tests() -> None:
    """Drop the cached client so tests can re-resolve after changing env."""
    global _client, _resolved
    _client = None
    _resolved = False


# ─────────────────────────────────────────────────────────────────────
# Handles — thin wrappers so call sites stay clean whether tracing is on or off
# ─────────────────────────────────────────────────────────────────────


class _NoopSpan:
    def set_output(self, **_kw: Any) -> None: ...


class _NoopGen:
    def succeed(self, **_kw: Any) -> None: ...
    def degrade(self, **_kw: Any) -> None: ...
    def fail(self, **_kw: Any) -> None: ...


class _Span:
    def __init__(self, span: Any) -> None:
        self._span = span

    def set_output(self, *, output: Any = None, metadata: Any = None) -> None:
        try:
            self._span.update(output=output, metadata=metadata)
        except Exception:  # noqa: BLE001
            logger.debug("span.update failed", exc_info=True)


class _Gen:
    """One agent's LLM call. Exactly one of succeed/degrade/fail is called."""

    def __init__(self, gen: Any) -> None:
        self._gen = gen

    def _update(self, *, output: Any, level: str, status: str | None,
                usage: dict[str, int] | None, cost: float | None) -> None:
        try:
            self._gen.update(
                output=output,
                level=level,
                status_message=status,
                usage_details=usage,
                cost_details=({"total": cost} if cost is not None else None),
            )
        except Exception:  # noqa: BLE001
            logger.debug("generation.update failed", exc_info=True)

    def succeed(self, *, output: Any = None, usage: dict[str, int] | None = None,
                cost: float | None = None) -> None:
        self._update(output=output, level="DEFAULT", status=None, usage=usage, cost=cost)

    def degrade(self, *, output: Any = None, status: str = "ran on a retry / fallback",
                usage: dict[str, int] | None = None, cost: float | None = None) -> None:
        self._update(output=output, level="WARNING", status=status, usage=usage, cost=cost)

    def fail(self, *, status: str, usage: dict[str, int] | None = None,
             cost: float | None = None) -> None:
        self._update(output=None, level="ERROR", status=status, usage=usage, cost=cost)


# ─────────────────────────────────────────────────────────────────────
# Context managers — the two things the council opens
# ─────────────────────────────────────────────────────────────────────


@contextmanager
def council_trace(
    *,
    symbol: str,
    horizon: str,
    user_id: str | None = None,
    decision_id: str | None = None,
) -> Iterator[_Span]:
    """The parent observation for one council pass. Agent generations opened
    inside nest under it via OTEL context."""
    client = _resolve_client()
    if client is None:
        yield _NoopSpan()  # type: ignore[misc]
        return
    try:
        cm = client.start_as_current_observation(
            name=f"council:{symbol}",
            as_type="chain",
            input={"symbol": symbol, "horizon": horizon},
            metadata={"user_id": user_id, "decision_id": decision_id},
        )
    except Exception:  # noqa: BLE001
        logger.debug("council_trace start failed", exc_info=True)
        yield _NoopSpan()  # type: ignore[misc]
        return
    with cm as span:
        yield _Span(span)


@contextmanager
def agent_generation(*, role: str, model: str, system: str, user: str) -> Iterator[_Gen]:
    """One agent node's LLM call. ``role`` is the agent name shown in Langfuse
    (router / technical / fundamental / macro / selector / drafter / reflection)."""
    client = _resolve_client()
    if client is None:
        yield _NoopGen()  # type: ignore[misc]
        return
    try:
        cm = client.start_as_current_observation(
            name=role,
            as_type="generation",
            model=model.split("+", 1)[0],  # strip the "+mock" suffix
            input={"system": system[:2000], "user": user[:4000]},
        )
    except Exception:  # noqa: BLE001
        logger.debug("agent_generation start failed", exc_info=True)
        yield _NoopGen()  # type: ignore[misc]
        return
    with cm as gen:
        yield _Gen(gen)


def flush() -> None:
    """Export buffered spans. Long-lived processes rely on the SDK's
    background flush; short-lived ones (cron) call this in a finally."""
    client = _resolve_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:  # noqa: BLE001
        logger.debug("Langfuse flush failed", exc_info=True)
