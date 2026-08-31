"""Contract funnel aggregation — /api/v1/insights/funnel.

Every options council pass (approved or refused) writes a
``contract_funnel`` block into ``agent_decisions.reasoning`` (JSONB) —
stage survivor counts, a ``rejection_reason``, and ``selected_occ``. Until
now nothing read it back; ``select_contract`` (``engine.options.selection``)
is where MOST options refusals happen, and every one of them was
``logger.info``'d and thrown away.

Scoped by a REQUIRED ``user_id`` via the same ``ghost_service._tenant_filters``
helper the ghost/veto builders use — no second tenant-scoping implementation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select

from app.services.council.ghost_service import _NoSuchTenant, _tenant_filters
from engine.db import async_session_factory
from engine.db.models import AgentDecision
from engine.options.selection import _STAGE_REJECTION_REASONS

# ── Stage order + labels ────────────────────────────────────────────────
# Order is DERIVED from ``_STAGE_REJECTION_REASONS`` — the dict that
# actually drives ``select_contract``'s filter order — rather than
# retyped here, so a reordering there can't silently desync this view
# from the code that produces the data (CLAUDE.md §4.4: the same
# ordering in two places will drift). "total" is prepended: it is always
# the first count ``select_contract`` records, but it has no rejection
# reason of its own — a total of 0 is "no_candidates", not one of the six
# named stage reasons.
_TOTAL_STAGE = "total"
_STAGE_ORDER: tuple[str, ...] = (_TOTAL_STAGE, *_STAGE_REJECTION_REASONS.keys())

_STAGE_LABELS: dict[str, str] = {
    _TOTAL_STAGE: "Contracts in chain",
    "contract_type": "Calls (or puts)",
    "dte_window": "10–45 DTE",
    "delta_band": "In the delta band",
    "liquidity": "OI ≥ 100 · spread ≤ 12%",
    "iv_present": "IV reported",
    "iv_realized_vol_band": "IV sane vs realized",
}


@dataclass(frozen=True)
class FunnelStage:
    key: str
    label: str
    survivors: int
    dropped: int


@dataclass(frozen=True)
class FunnelRun:
    decision_id: str
    symbol: str
    triggered_at: datetime
    stages: list[FunnelStage]
    rejection_reason: str | None
    rejection_stage: str | None
    """Which stage's count hit zero — derived from the counts themselves,
    not from ``rejection_reason``, so it stays correct even for the
    "no_candidates" case (a zero ``total``), which isn't one of the six
    named reasons in ``_STAGE_REJECTION_REASONS`` at all."""
    selected_occ: str | None
    outcome: str
    """"bought" | "held". "bought" only when the pass's ``final_action``
    is "BUY" — a contract can survive the whole funnel and still end up
    "held" if the sizer floors to 0 contracts or risk vetoes it
    afterward (``final_action`` becomes "VETOED" in that case); both are
    represented here as "held" because neither bought anything."""


@dataclass(frozen=True)
class FunnelAggregate:
    """Summed across the window — the headline number."""

    stages: list[FunnelStage]
    runs: int
    bought: int
    top_rejection_reasons: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class FunnelReport:
    window_days: int
    aggregate: FunnelAggregate
    recent: list[FunnelRun] = field(default_factory=list)


def _empty_report(window_days: int) -> FunnelReport:
    """Zeroed report — what an unknown tenant, or an empty window, sees."""
    return FunnelReport(
        window_days=window_days,
        aggregate=FunnelAggregate(stages=[], runs=0, bought=0, top_rejection_reasons=[]),
        recent=[],
    )


def _extract_funnel(reasoning: object) -> dict[str, Any] | None:
    """``reasoning['contract_funnel']`` if usable, else ``None``.

    Tolerance rule: a row with no ``reasoning``, no ``contract_funnel``, or
    a non-dict value for either is treated as carrying no funnel at all —
    skipped by the caller, never raised. This JSONB column holds several
    generations of payload shape; one malformed row must not take down
    the whole report.
    """
    if not isinstance(reasoning, dict):
        return None
    funnel = reasoning.get("contract_funnel")
    if not isinstance(funnel, dict):
        return None
    return funnel


def _stage_counts(funnel: Mapping[str, Any]) -> dict[str, int]:
    """The int-valued entries of ``funnel['counts']`` — everything else
    (a missing/non-dict ``counts``, or a non-int value for a given stage)
    is dropped rather than coerced, so a malformed entry reads as ABSENT,
    not zero. ``bool`` is excluded despite being an ``int`` subclass in
    Python — a stray ``True``/``False`` is not a survivor count.
    """
    counts = funnel.get("counts")
    if not isinstance(counts, dict):
        return {}
    return {
        key: value
        for key, value in counts.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _build_stages(counts_list: Sequence[Mapping[str, int]]) -> list[FunnelStage]:
    """Stage list for one run (a single-element ``counts_list``) or for
    the whole-window aggregate (every run's counts).

    A stage key ABSENT from a given run's counts contributes NOTHING to
    that stage's sum — not zero. Absent means "this generation of the
    code never emitted this stage"; zero means "it ran and nothing
    survived" — conflating the two would make an old row look like a
    100% rejection at a stage it never even computed. A stage no row
    reports at all is left out of the result entirely, and ``dropped``
    for the next reported stage is measured against the last stage that
    WAS reported, never against a synthesized zero. ``dropped`` is
    clamped at 0 so malformed data (a count that rises between stages)
    can never read as a negative drop.
    """
    sums: dict[str, int] = {}
    seen: set[str] = set()
    for counts in counts_list:
        for key in _STAGE_ORDER:
            if key not in counts:
                continue
            sums[key] = sums.get(key, 0) + counts[key]
            seen.add(key)

    stages: list[FunnelStage] = []
    prev: int | None = None
    for key in _STAGE_ORDER:
        if key not in seen:
            continue
        survivors = sums[key]
        dropped = max(0, prev - survivors) if prev is not None else 0
        stages.append(
            FunnelStage(key=key, label=_STAGE_LABELS[key], survivors=survivors, dropped=dropped)
        )
        prev = survivors
    return stages


def _rejection_stage(counts: Mapping[str, int]) -> str | None:
    """The FIRST stage (in fixed evaluation order) whose count is exactly
    zero. Independent of whatever ``rejection_reason`` says, so it stays
    right even when that string is absent, stale, or (for "no_candidates")
    not one of the six named reasons at all. An absent key is not zero and
    is skipped, matching ``select_contract``'s own "first stage that hit
    zero names the reason" rule.
    """
    for key in _STAGE_ORDER:
        if counts.get(key) == 0:
            return key
    return None


def _outcome(final_action: object) -> str:
    return "bought" if final_action == "BUY" else "held"


def _clean_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def build_funnel_report_from_rows(
    rows: Sequence[Mapping[str, Any]], *, window_days: int, limit: int = 20
) -> FunnelReport:
    """Pure aggregation, no I/O — split out from ``build_funnel_report`` so
    it is testable without a database, mirroring
    ``ghost_service.count_trim_rules``.

    ``rows`` are plain mappings carrying ``id``, ``symbol``,
    ``triggered_at``, ``final_action``, ``reasoning`` — newest first.
    ``aggregate`` sums over every usable row in the window; ``recent`` is
    capped at ``limit`` (the first ``limit`` rows in the given order).
    """
    all_counts: list[dict[str, int]] = []
    reason_counts: dict[str, int] = {}
    bought = 0
    runs: list[FunnelRun] = []

    for row in rows:
        funnel = _extract_funnel(row.get("reasoning"))
        if funnel is None:
            continue

        counts = _stage_counts(funnel)
        all_counts.append(counts)

        reason = _clean_str(funnel.get("rejection_reason"))
        if reason is not None:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

        outcome = _outcome(row.get("final_action"))
        if outcome == "bought":
            bought += 1

        if len(runs) < limit:
            triggered_at = cast(datetime, row.get("triggered_at"))
            runs.append(
                FunnelRun(
                    decision_id=str(row.get("id")),
                    symbol=str(row.get("symbol")),
                    triggered_at=triggered_at,
                    stages=_build_stages([counts]),
                    rejection_reason=reason,
                    rejection_stage=_rejection_stage(counts),
                    selected_occ=_clean_str(funnel.get("selected_occ")),
                    outcome=outcome,
                )
            )

    top_rejection_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    return FunnelReport(
        window_days=window_days,
        aggregate=FunnelAggregate(
            stages=_build_stages(all_counts),
            runs=len(all_counts),
            bought=bought,
            top_rejection_reasons=top_rejection_reasons,
        ),
        recent=runs,
    )


async def build_funnel_report(
    window_days: int = 30, *, user_id: str, limit: int = 20
) -> FunnelReport:
    """Contract-funnel report for ``user_id`` over ``window_days``.

    Reads ``reasoning->'contract_funnel'`` across the window, scoped by
    ``ghost_service._tenant_filters``. Aggregation happens in Python (see
    module + impl-doc §1.1) — the row count is one window of one tenant's
    decisions, and a JSONB ``->>`` predicate here would be one more place
    for a key name to drift from ``runtime._reasoning_block``.
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    try:
        tenant = _tenant_filters(user_id)
    except _NoSuchTenant:
        return _empty_report(window_days)

    session_factory = async_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(
                AgentDecision.id,
                AgentDecision.symbol,
                AgentDecision.triggered_at,
                AgentDecision.final_action,
                AgentDecision.reasoning,
            )
            .where(
                AgentDecision.triggered_at >= cutoff,
                AgentDecision.reasoning.is_not(None),
                *tenant,
            )
            .order_by(AgentDecision.triggered_at.desc())
        )
        rows = result.all()

    mapped = [
        {
            "id": row.id,
            "symbol": row.symbol,
            "triggered_at": row.triggered_at,
            "final_action": row.final_action,
            "reasoning": row.reasoning,
        }
        for row in rows
    ]
    return build_funnel_report_from_rows(mapped, window_days=window_days, limit=limit)
