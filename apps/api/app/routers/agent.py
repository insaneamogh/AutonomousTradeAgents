"""/api/v1/agent — trigger a council run.

POST /api/v1/agent/run
    Synchronous: awaits the full council, returns AgentRunResponse.
    Kept for the cron path, tests, and any client that doesn't need
    the theater.

POST /api/v1/agent/run/start          → 202 {runId}
GET  /api/v1/agent/run/{id}/progress  → polled theater feed
    Background run + per-node progress events. The mobile theater screen
    polls progress every ~600ms while status == "running".

When the council approves a trade, the proposal lands in the pending
queue (via the unified agent_decisions row on Postgres) + a push
notification fans out.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, status as http_status

from app.core.config import get_settings
from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.agent import (
    AgentRunRequest,
    AgentRunResponse,
    AgentRunStartResponse,
    CouncilProgressEvent,
    CouncilProgressResponse,
)
from app.schemas.approvals import ApprovalProposalDto
from app.services.agent_runs import get_run_registry
from app.services.notifications import schedule_proposal_pending_notification
from app.services.store import get_store
from trading_agents.memory import get_confidence_store, get_decision_log
from trading_agents.progress import ProgressEvent
from trading_agents.runtime import run_council

logger = logging.getLogger("api.router.agent")

router = APIRouter(prefix="/agent", tags=["agent"])


def _postgres_active() -> bool:
    """Mirror of the store/memory factories' env switch."""
    v = os.environ.get("USE_POSTGRES")
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


async def _execute_council(
    body: AgentRunRequest,
    user: AuthedUser,
    progress_cb: Callable[[ProgressEvent], Awaitable[None]] | None = None,
) -> AgentRunResponse:
    """Shared council execution — sync route and theater runner both land here.

    The decision log persists ONE row per run — vetoed and HOLD runs
    included — so the veto ledger / ghost P&L / biography features have a
    complete audit trail. When Postgres is active that row doubles as the
    pending-approvals entry (its ``proposal`` JSONB is the camelCase DTO),
    so we skip the legacy ``append_pending`` write to avoid duplicates.

    NOTE Phase-0 single-user semantics: PostgresStore reads account /
    activity / pending under the fixture user, so the decision row must
    land there too (user_id=None → FIXTURE_USER_ID in the log). The
    authed user's id is kept in logs only. Per-user stores are the
    Phase 3+ migration — change both sides together.
    """
    result = await run_council(
        symbol=body.symbol,
        horizon=body.horizon,
        user_id=None,
        decision_log=get_decision_log(),
        confidence_store=get_confidence_store(),
        progress_cb=progress_cb,
        pacing_seconds=get_settings().theater_mock_pacing_seconds,
    )

    proposal_dto: ApprovalProposalDto | None = None
    if result["proposal"] is not None:
        # The runtime emits camelCase keys; populate_by_name=True lets Pydantic accept them.
        proposal_dto = ApprovalProposalDto.model_validate(result["proposal"])
        if not _postgres_active():
            # In-memory decision log (MockStore mode) — that row isn't
            # queryable by list_pending, so keep the legacy write.
            store = get_store()
            await store.append_pending(proposal_dto)
        logger.info(
            "council proposed %s %s qty=%d for user=%s (decision_id=%s)",
            proposal_dto.side, proposal_dto.symbol, proposal_dto.qty, user.id,
            result.get("decision_id"),
        )
        # Fire-and-forget push fan-out — logged + swallowed on failure;
        # the council route never 5xx's because a push token timed out.
        schedule_proposal_pending_notification(
            user_id=user.id,
            proposal=result["proposal"],
        )

    return AgentRunResponse(
        proposal=proposal_dto,
        final_action=result["final_action"],
        risk_approved=result["risk_approved"],
        risk_reason=result["risk_reason"],
        risk_veto_rule=result.get("risk_veto_rule"),
        regime=result.get("regime"),
        llm_mock=result["llm_mock"],
    )


@router.post("/run", response_model=AgentRunResponse, response_model_by_alias=True)
async def run(
    body: AgentRunRequest,
    user: AuthedUser = Depends(get_current_user),
) -> AgentRunResponse:
    """Run the council synchronously. Auth-gated like the rest of v1."""
    return await _execute_council(body, user)


@router.post(
    "/run/start",
    response_model=AgentRunStartResponse,
    response_model_by_alias=True,
    status_code=http_status.HTTP_202_ACCEPTED,
)
async def run_start(
    body: AgentRunRequest,
    user: AuthedUser = Depends(get_current_user),
) -> AgentRunStartResponse:
    """Launch a council run in the background; poll /run/{id}/progress.

    One concurrent run per user — a double-tap returns the in-flight runId
    instead of double-spending LLM calls.
    """
    registry = get_run_registry()

    async def _runner(cb: Callable[[ProgressEvent], Awaitable[None]]) -> dict[str, Any]:
        resp = await _execute_council(body, user, progress_cb=cb)
        return resp.model_dump(by_alias=True, mode="json")

    rec = registry.start(user_id=user.id, symbol=body.symbol.upper(), runner=_runner)
    return AgentRunStartResponse(run_id=rec.run_id, symbol=rec.symbol)


@router.get(
    "/run/{run_id}/progress",
    response_model=CouncilProgressResponse,
    response_model_by_alias=True,
)
async def run_progress(
    run_id: str,
    after: int = 0,
    user: AuthedUser = Depends(get_current_user),
) -> CouncilProgressResponse:
    """Polled theater feed. ``after`` is the last seq the client has seen."""
    rec = get_run_registry().get(run_id)
    if rec is None or rec.user_id != user.id:
        raise HTTPException(status_code=404, detail="run not found (expired or unknown)")

    events = [
        CouncilProgressEvent.model_validate(e) for e in rec.events if int(e["seq"]) > after
    ]
    result = (
        AgentRunResponse.model_validate(rec.result)
        if rec.status == "completed" and rec.result is not None
        else None
    )
    return CouncilProgressResponse(
        run_id=rec.run_id,
        status=rec.status,
        events=events,
        result=result,
        error=rec.error,
    )
