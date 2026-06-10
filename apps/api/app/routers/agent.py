"""/api/v1/agent — trigger a council run.

POST /api/v1/agent/run
    body: { symbol: str, horizon?: "intraday"|"short"|"mid"|"long" }
    → AgentRunResponse with the produced proposal (if approved) or null
      plus the risk reason / regime / mock-mode flag.

When the council approves a trade, the proposal is appended to the pending
queue + an activity entry is recorded so the mobile feed reflects it.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.schemas.approvals import ApprovalProposalDto
from app.services.notifications import schedule_proposal_pending_notification
from app.services.store import get_store
from trading_agents.memory import get_confidence_store, get_decision_log
from trading_agents.runtime import run_council

logger = logging.getLogger("api.router.agent")

router = APIRouter(prefix="/agent", tags=["agent"])


def _postgres_active() -> bool:
    """Mirror of the store/memory factories' env switch."""
    v = os.environ.get("USE_POSTGRES")
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


@router.post("/run", response_model=AgentRunResponse, response_model_by_alias=True)
async def run(
    body: AgentRunRequest,
    user: AuthedUser = Depends(get_current_user),
) -> AgentRunResponse:
    """Run the council for a single symbol. Auth-gated like the rest of v1.

    The decision log persists ONE row per run — vetoed and HOLD runs
    included — so the veto ledger / ghost P&L / biography features have a
    complete audit trail. When Postgres is active that row doubles as the
    pending-approvals entry (its ``proposal`` JSONB is the camelCase DTO),
    so we skip the legacy ``append_pending`` write to avoid duplicates.
    """
    # NOTE Phase-0 single-user semantics: PostgresStore reads account /
    # activity / pending under the fixture user, so the decision row must
    # land there too (user_id=None → FIXTURE_USER_ID in the log). The
    # authed user's id is kept in logs only. Per-user stores are the
    # Phase 3+ migration — change both sides together.
    result = await run_council(
        symbol=body.symbol,
        horizon=body.horizon,
        user_id=None,
        decision_log=get_decision_log(),
        confidence_store=get_confidence_store(),
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
        # Fire-and-forget push fan-out. The route returns immediately; the
        # fan-out runs in a detached task. Anything that goes wrong inside
        # the task is logged + swallowed — the council route never 5xx's
        # because one user's stale push token timed out.
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
