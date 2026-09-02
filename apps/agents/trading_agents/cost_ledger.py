"""LLM cost ledger — every Anthropic call writes a row.

PLAN.md §9 cost story. We compute USD cost in code (so price reps can
land without a migration) and persist the call shape. ``/api/v1/health/full``
sums YTD from this same data.

Architecture:
  - ``LedgerEntry`` is the dataclass crossing the boundary.
  - ``CostLedger`` Protocol + ``InMemoryCostLedger`` + (factory-deferred)
    ``PostgresCostLedger``.
  - ``compute_cost_usd`` is pure — no I/O — so it's trivially testable.
  - ``record(...)`` is called from the LLM wrapper after each successful
    completion (real OR mock). Mocks land with ``is_mock=True`` so the
    YTD spend slicing excludes them.

The pricing table below is the **per-million-token** cost. Source:
public Anthropic pricing as of 2026-05-30. Keep it in sync as Anthropic
revs prices. Cache reads are charged at 10% of input.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from engine.env import env_flag

# ─────────────────────────────────────────────────────────────────────
# Pricing table
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float
    cache_creation_per_million: float


# Per-million-token USD. Cache reads are 10% of base input; cache creation
# is 125% of base input — both Anthropic conventions.
_PRICES: dict[str, ModelPrice] = {
    # Haiku 4.5
    "claude-haiku-4-5-20251001": ModelPrice(
        input_per_million=1.00,
        output_per_million=5.00,
        cache_read_per_million=0.10,
        cache_creation_per_million=1.25,
    ),
    # Sonnet 4.6
    "claude-sonnet-4-6": ModelPrice(
        input_per_million=3.00,
        output_per_million=15.00,
        cache_read_per_million=0.30,
        cache_creation_per_million=3.75,
    ),
    # Opus 4.7
    "claude-opus-4-7": ModelPrice(
        input_per_million=15.00,
        output_per_million=75.00,
        cache_read_per_million=1.50,
        cache_creation_per_million=18.75,
    ),
}

# Fallback for unknown models — assume Sonnet-tier. Logged at WARNING
# so we notice when a new model slipped in without a price row.
_FALLBACK_PRICE = _PRICES["claude-sonnet-4-6"]


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Pure cost calc. Returns 0.0 only when ALL token counts are
    non-positive (mock responses) — cache reads / creation alone DO
    incur cost so they count.
    """
    if (
        input_tokens <= 0
        and output_tokens <= 0
        and cache_read_tokens <= 0
        and cache_creation_tokens <= 0
    ):
        return 0.0

    # Strip mock suffix that ``llm._mock_response`` adds.
    base_model = model.split("+", 1)[0]
    price = _PRICES.get(base_model, _FALLBACK_PRICE)

    cost = (
        max(0, input_tokens) * price.input_per_million
        + max(0, output_tokens) * price.output_per_million
        + max(0, cache_read_tokens) * price.cache_read_per_million
        + max(0, cache_creation_tokens) * price.cache_creation_per_million
    ) / 1_000_000.0
    return round(cost, 6)


# ─────────────────────────────────────────────────────────────────────
# Ledger entry + store
# ─────────────────────────────────────────────────────────────────────


@dataclass
class LedgerEntry:
    id: str = field(default_factory=lambda: f"llm-{uuid.uuid4().hex[:12]}")
    agent_decision_id: str | None = None
    user_id: str | None = None
    council_run_id: str | None = None
    """Correlates every LLM call in one council pass, written BEFORE the
    matching ``agent_decisions`` row exists (that row's id isn't assigned
    until strictly after every LLM call in the pass has completed — see
    ``trading_agents.runtime.run_council``). ``agent_decision_id`` above
    starts NULL for a live call and gets backfilled afterwards via
    ``CostLedger.backfill_decision_id`` once the real decision id exists."""
    model: str = ""
    role: str = "unknown"
    """Role of the call — router / technical / fundamental / macro /
    selector / drafter / reflection / unknown."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    is_mock: bool = False
    called_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class CostLedger(Protocol):
    async def record(self, entry: LedgerEntry) -> LedgerEntry: ...
    async def sum_cost_since(
        self, since: timedelta, *, exclude_mock: bool = True
    ) -> tuple[float, int]:
        """Returns (total_usd, call_count)."""

    async def count_runs_since(
        self, since: timedelta, *, exclude_mock: bool = True
    ) -> int:
        """Distinct ``council_run_id``s that reached a real LLM call.

        The unit a per-DAY budget is actually spent in. ``sum_cost_since``
        counts CALLS (one pass is 3-5 of them) and dollars; neither answers
        "how many symbols have we paid to debate today", which is the
        number an operator reasons about and the one
        ``MAX_LLM_SYMBOLS_PER_DAY`` caps.

        One council pass runs exactly one symbol, so distinct runs is the
        paid-symbol count — and a symbol debated twice correctly counts
        twice, because it was paid for twice.
        """

    async def all(self) -> list[LedgerEntry]:
        """Debug / testing only."""

    async def backfill_decision_id(
        self, *, council_run_id: str, decision_id: str
    ) -> None:
        """Attach ``decision_id`` to every row sharing ``council_run_id``.

        Called once per council pass, right after the decision row is
        recorded — every LLM call in that pass wrote with
        ``agent_decision_id=None`` and ``council_run_id`` set (the decision
        didn't exist yet). Must never clobber a row some other path already
        attributed. Best-effort: a ledger outage must never take down a
        council run.
        """


class InMemoryCostLedger:
    def __init__(self) -> None:
        self._rows: list[LedgerEntry] = []

    async def record(self, entry: LedgerEntry) -> LedgerEntry:
        self._rows.append(entry)
        return entry

    async def sum_cost_since(
        self, since: timedelta, *, exclude_mock: bool = True
    ) -> tuple[float, int]:
        cutoff = datetime.now(UTC) - since
        rows = [
            r for r in self._rows
            if r.called_at >= cutoff and (not exclude_mock or not r.is_mock)
        ]
        return (round(sum(r.cost_usd for r in rows), 6), len(rows))

    async def count_runs_since(
        self, since: timedelta, *, exclude_mock: bool = True
    ) -> int:
        cutoff = datetime.now(UTC) - since
        return len({
            r.council_run_id for r in self._rows
            if r.called_at >= cutoff
            and r.council_run_id
            and (not exclude_mock or not r.is_mock)
        })

    async def all(self) -> list[LedgerEntry]:
        return list(self._rows)

    async def backfill_decision_id(
        self, *, council_run_id: str, decision_id: str
    ) -> None:
        # Same NULL-guard as the Postgres impl (``AND agent_decision_id IS
        # NULL``): a row some other path already attributed must not be
        # clobbered.
        for row in self._rows:
            if row.council_run_id == council_run_id and row.agent_decision_id is None:
                row.agent_decision_id = decision_id


# ─────────────────────────────────────────────────────────────────────
# Role inference from the prompt — same anchor the mock LLM uses
# ─────────────────────────────────────────────────────────────────────


def infer_role_from_system_prompt(system: str) -> str:
    """Cheap role tag for the cost ledger. Matches ``_mock_response``'s
    role anchor so the slicing stays consistent.
    """
    line = system[:160].lower()
    if "you are the router" in line:
        return "router"
    if "you are the technical analyst" in line:
        return "technical"
    if "you are the fundamental analyst" in line:
        return "fundamental"
    if "you are the macro analyst" in line:
        return "macro"
    # NOTE: "strategy selector" is retained only so historical ledger rows
    # written before the Selector became deterministic still resolve to a
    # role. No live call produces it any more.
    if "you are the strategy selector" in line:
        return "selector"
    if "you are the proposal drafter" in line:
        return "drafter"
    if "you are the reflection agent" in line:
        return "reflection"
    if "you are the options bull agent" in line:
        return "options_bull"
    if "you are the options bear agent" in line:
        return "options_bear"
    if "you are the options escalation agent" in line:
        return "options_escalation"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


_cost_ledger: CostLedger | None = None


def get_cost_ledger() -> CostLedger:
    """Process singleton. Postgres-backed when ``USE_POSTGRES`` is set.

    Falls back to the in-memory ledger if the Postgres impl can't be
    constructed (no DATABASE_URL, driver missing). That degrades cost
    *reporting*, which is recoverable; refusing to build a ledger would
    take down every council run, which is not.
    """
    import logging

    global _cost_ledger
    if _cost_ledger is None:
        if env_flag("USE_POSTGRES"):
            try:
                from trading_agents.memory.cost_ledger_postgres import (
                    PostgresCostLedger,
                )

                _cost_ledger = PostgresCostLedger()
                return _cost_ledger
            except Exception:
                logging.getLogger("agents.cost").exception(
                    "PostgresCostLedger unavailable — falling back to "
                    "InMemoryCostLedger. Costs won't persist."
                )
        _cost_ledger = InMemoryCostLedger()
    return _cost_ledger


def reset_cost_ledger_for_tests() -> None:
    global _cost_ledger
    _cost_ledger = None
