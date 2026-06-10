"""Review service — bridges DecisionLog + StrategyConfidenceStore + ReviewStore.

Three operations the router orchestrates:

  1. ``build_queue(operator_user_id, window_days)`` —
     Returns review-ready items = decisions in window with
     ``realized_pnl IS NOT NULL`` that the operator hasn't graded yet.

  2. ``apply_grade(operator_user_id, decision_id, grade, notes)`` —
     Upsert into ReviewStore. The agent_decisions row isn't mutated;
     reviews live on a separate table to keep the audit log clean.

  3. ``build_agreement(operator_user_id, window_days)`` —
     For every graded decision, look at the matching strategy's most-recent
     reflection nudge direction (sign of (current_confidence - 0.5)) and
     bucket. Compute agreement_pct. Used by the Home strip's "calibration"
     widget so the operator sees whether Reflection tracks their view.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from trading_agents.memory import get_confidence_store, get_decision_log

from app.schemas.review import (
    AgreementBucket,
    AgreementResponse,
    GradeResponse,
    OverrideStats,
    ReviewQueueItem,
    ReviewQueueResponse,
    ScorecardMonth,
    ScorecardResponse,
)
from app.schemas.review import (
    Grade as GradeLiteral,
)
from app.services.review_store import (
    DecisionReviewRecord,
    Grade,
    ReviewStore,
    get_review_store,
)


def _now() -> datetime:
    return datetime.now(UTC)


# ─────────────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────────────


async def build_queue(
    *,
    operator_user_id: str,
    window_days: int,
    review_store: ReviewStore | None = None,
) -> ReviewQueueResponse:
    rs = review_store or get_review_store()
    log = get_decision_log()
    cutoff = _now() - timedelta(days=window_days)

    all_decisions = await log.all_decisions()
    in_window_completed = [
        d for d in all_decisions
        if d.triggered_at >= cutoff
        and d.realized_pnl is not None
    ]

    operator_reviews = await rs.list_reviews_for_operator(operator_user_id)
    graded_ids = {r.decision_id for r in operator_reviews}

    queue_items: list[ReviewQueueItem] = []
    for d in in_window_completed:
        if d.id in graded_ids:
            continue
        proposal = d.raw_state.get("proposal") if isinstance(d.raw_state, dict) else None
        queue_items.append(
            ReviewQueueItem(
                decision_id=d.id,
                triggered_at=d.triggered_at,
                symbol=d.symbol,
                side=d.final_action,
                qty=(proposal or {}).get("qty") if proposal else None,
                fill_qty=d.fill_qty,
                fill_avg_price=d.fill_avg_price,
                realized_pnl=d.realized_pnl,
                selected_strategy=d.selected_strategy,
                selector_confidence=d.selector_confidence,
                bull_case=str((proposal or {}).get("bull_case", "")) if proposal else "",
                bear_case=str((proposal or {}).get("bear_case", "")) if proposal else "",
                regime=d.regime,
            )
        )

    # Stable order: oldest first so the operator works chronologically
    # — easier to reason about correlated days.
    queue_items.sort(key=lambda x: x.triggered_at)

    return ReviewQueueResponse(
        items=queue_items,
        total_in_window=len(in_window_completed),
        graded_in_window=sum(
            1 for r in operator_reviews
            if any(d.id == r.decision_id for d in in_window_completed)
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Grade upsert
# ─────────────────────────────────────────────────────────────────────


class ReviewError(Exception):
    pass


class DecisionNotReviewable(ReviewError):
    """Raised when the operator tries to grade a decision that doesn't
    exist OR that's still open (no realized_pnl yet)."""


async def apply_grade(
    *,
    operator_user_id: str,
    decision_id: str,
    grade: GradeLiteral,
    notes: str | None,
    review_store: ReviewStore | None = None,
) -> GradeResponse:
    rs = review_store or get_review_store()

    # Confirm the decision actually exists + is reviewable (has a fill).
    log = get_decision_log()
    decisions = await log.all_decisions()
    target = next((d for d in decisions if d.id == decision_id), None)
    if target is None:
        raise DecisionNotReviewable(f"no decision with id={decision_id!r}")
    if target.realized_pnl is None:
        raise DecisionNotReviewable(
            f"decision {decision_id} has no realized_pnl yet — not reviewable"
        )

    rec = await rs.upsert_review(
        decision_id=decision_id,
        operator_user_id=operator_user_id,
        grade=cast(Grade, grade),
        notes=notes,
    )
    return _to_grade_response(rec)


# ─────────────────────────────────────────────────────────────────────
# Agreement stat
# ─────────────────────────────────────────────────────────────────────


async def build_agreement(
    *,
    operator_user_id: str,
    window_days: int,
    review_store: ReviewStore | None = None,
) -> AgreementResponse:
    rs = review_store or get_review_store()
    cutoff = _now() - timedelta(days=window_days)

    reviews = [
        r for r in await rs.list_reviews_for_operator(operator_user_id)
        if r.reviewed_at >= cutoff
    ]

    # Index decisions by id for fast strategy lookup.
    log = get_decision_log()
    decisions_by_id = {d.id: d for d in await log.all_decisions()}

    confidence_store = get_confidence_store()
    priors_by_strategy = {row.strategy_id: row.confidence for row in await confidence_store.all()}

    counts: dict[tuple[Grade, Literal["positive", "negative", "neutral"]], int] = (
        defaultdict(int)
    )
    agreeing = 0
    counted = 0

    for r in reviews:
        decision = decisions_by_id.get(r.decision_id)
        if decision is None or decision.selected_strategy is None:
            continue
        prior = priors_by_strategy.get(decision.selected_strategy, 0.5)
        direction: Literal["positive", "negative", "neutral"]
        if prior > 0.52:
            direction = "positive"
        elif prior < 0.48:
            direction = "negative"
        else:
            direction = "neutral"

        counts[(r.grade, direction)] += 1

        # ``skip`` doesn't contribute to agreement.
        if r.grade == "skip":
            continue
        counted += 1
        if (r.grade == "good" and direction == "positive") or (r.grade == "bad" and direction == "negative"):
            agreeing += 1

    buckets = [
        AgreementBucket(
            operator_grade=cast(GradeLiteral, grade),
            reflection_direction=direction,
            count=count,
        )
        for (grade, direction), count in sorted(counts.items())
    ]

    return AgreementResponse(
        window_days=window_days,
        total_reviewed=len(reviews),
        agreement_pct=(agreeing / counted * 100) if counted else 0.0,
        buckets=buckets,
    )


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _to_grade_response(rec: DecisionReviewRecord) -> GradeResponse:
    return GradeResponse(
        id=rec.id,
        decision_id=rec.decision_id,
        grade=cast(GradeLiteral, rec.grade),
        notes=rec.notes,
        reviewed_at=rec.reviewed_at,
    )

# ─────────────────────────────────────────────────────────────────────
# Calibration scorecard (WP5)
# ─────────────────────────────────────────────────────────────────────


def _direction_of(prior: float) -> Literal["positive", "negative", "neutral"]:
    if prior > 0.52:
        return "positive"
    if prior < 0.48:
        return "negative"
    return "neutral"


async def build_scorecard(
    *,
    operator_user_id: str,
    window_days: int = 180,
    review_store: ReviewStore | None = None,
) -> ScorecardResponse:
    """Monthly agreement buckets + override outcomes.

    Override = a graded decision where the operator's grade disagreed
    with the Reflection direction. When that decision has a realized
    P&L we can score who was right: the operator wins on (good ∧ pnl>0)
    or (bad ∧ pnl<=0); the reflection wins otherwise.

    Known compromise (documented in the plan): we use the strategy's
    CURRENT prior sign as the reflection-direction proxy — exact
    per-decision deltas need a reflection-events table (future).
    """
    rs = review_store or get_review_store()
    cutoff = _now() - timedelta(days=window_days)

    reviews = [
        r for r in await rs.list_reviews_for_operator(operator_user_id)
        if r.reviewed_at >= cutoff
    ]

    log = get_decision_log()
    decisions_by_id = {d.id: d for d in await log.all_decisions()}
    confidence_store = get_confidence_store()
    priors_by_strategy = {row.strategy_id: row.confidence for row in await confidence_store.all()}

    months: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "counted": 0, "agree": 0})
    operator_wins = reflection_wins = 0

    for r in reviews:
        decision = decisions_by_id.get(r.decision_id)
        if decision is None or decision.selected_strategy is None:
            continue
        direction = _direction_of(priors_by_strategy.get(decision.selected_strategy, 0.5))
        mkey = r.reviewed_at.strftime("%Y-%m")
        months[mkey]["total"] += 1
        if r.grade == "skip":
            continue
        months[mkey]["counted"] += 1
        agrees = (r.grade == "good" and direction == "positive") or (
            r.grade == "bad" and direction == "negative"
        )
        if agrees:
            months[mkey]["agree"] += 1
        elif decision.realized_pnl is not None:
            # Disagreement with a known outcome — score the override.
            pnl = float(decision.realized_pnl)
            operator_right = (r.grade == "good" and pnl > 0) or (r.grade == "bad" and pnl <= 0)
            if operator_right:
                operator_wins += 1
            else:
                reflection_wins += 1

    month_buckets = [
        ScorecardMonth(
            month=mkey,
            total_reviewed=v["total"],
            agreement_pct=(v["agree"] / v["counted"] * 100) if v["counted"] else 0.0,
        )
        for mkey, v in sorted(months.items())
    ]
    counted_total = sum(v["counted"] for v in months.values())
    agree_total = sum(v["agree"] for v in months.values())
    override_count = operator_wins + reflection_wins

    return ScorecardResponse(
        window_days=window_days,
        agreement_pct=(agree_total / counted_total * 100) if counted_total else 0.0,
        months=month_buckets,
        overrides=OverrideStats(
            count=override_count,
            operator_wins=operator_wins,
            reflection_wins=reflection_wins,
            operator_win_rate_pct=(operator_wins / override_count * 100) if override_count else 0.0,
        ),
    )
