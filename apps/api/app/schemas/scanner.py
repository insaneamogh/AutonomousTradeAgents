"""Wire schema for ``GET /api/v1/scanner/status``.

A dedicated response rather than an extension of ``HealthResponse``
(``schemas/health.py``): that schema is a lossy one-line-per-component
summary and can't cleanly carry a signal list. This endpoint's whole job
is to say WHICH deterministic rule fired, on which symbol, with what
detail — the same auditability principle
``packages/engine/engine/scanner/types.py`` documents for ``ScanSignal``
itself.

Uses ``CamelCaseModel`` (the shared alias generator in ``schemas/base.py``)
rather than rolling a bespoke camelCase config the way ``health.py`` does —
that duplication in ``health.py`` predates this file and isn't a pattern
worth repeating.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.base import CamelCaseModel


class AlpacaCliHealthDto(CamelCaseModel):
    """Health of the ``alpaca`` CLI subprocess integration."""

    available: bool
    """``alpaca version`` ran and exited 0 — the binary is present and
    executable. Checked with ``version`` rather than a trading call so
    this stays true even when credentials are wrong, which is what makes
    the two failures distinguishable."""
    enabled: bool
    """``USE_ALPACA_CLI`` is not explicitly off. Defaults on."""
    binary: str
    detail: str
    """Version string on success; the failure reason otherwise."""


class ScanSignalDto(CamelCaseModel):
    """One named trigger firing on one symbol. Mirrors
    ``engine.scanner.types.ScanSignal.as_dict()`` field-for-field."""

    symbol: str
    rule: str
    direction: Literal["bullish", "bearish"]
    strength: float
    detail: str
    observed_at: datetime
    context: dict[str, float | None] = {}


class ScannerStatusResponse(CamelCaseModel):
    """Full trigger-loop status. Four honest states a client can render
    unambiguously from these fields alone:

      1. Scheduler off            → ``scheduler_enabled=False``.
      2. Scheduler on, not armed  → ``scheduler_enabled=True`` and
         ``trigger_loop_armed=False``; ``scanner_enabled_flag`` tells
         apart "SCANNER_ENABLED=0" from "=1 but Alpaca keys missing".
      3. Armed and clean          → ``trigger_loop_armed=True`` and
         ``signals=[]``.
      4. Armed with signals       → ``trigger_loop_armed=True`` and
         ``signals`` non-empty.
    """

    scheduler_enabled: bool
    scanner_enabled_flag: bool
    trigger_loop_armed: bool
    market_open: bool | None
    market_open_source: str | None
    """``"alpaca_cli"`` (Alpaca's own CLI binary), ``"alpaca"`` (REST
    ``/v2/clock``) or ``"local_calendar"`` (holiday-table fallback) — which
    source answered the last scan's market-hours check. ``None`` before any
    scan has run, same as ``market_open`` itself."""
    alpaca_cli: AlpacaCliHealthDto | None = None
    """Whether Alpaca's own CLI binary is present and enabled.

    Exists to make the hackathon's "projects must utilize either Alpaca's
    MCP server or its CLI tools" requirement VISIBLE rather than
    inferable. ``market_open_source == "alpaca_cli"`` already proves the
    CLI answered, but only after a scan has run and only to someone who
    knows to look for that string; this states it outright, and
    distinguishes "binary missing" from "binary present, flag off" from
    "present and running" — three states with three different fixes."""
    last_scan_at: datetime | None
    scan_interval_minutes: int | None
    max_council_runs_per_scan: int | None
    watchlist_size: int
    signals: list[ScanSignalDto]
    triggered_symbols: list[str]
    suppressed_count: int
    last_council_run_at: datetime | None
    last_council_run_symbols: list[str]
    generated_at: datetime
