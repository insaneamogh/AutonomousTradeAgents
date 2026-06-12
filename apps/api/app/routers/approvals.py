"""/api/v1/approvals — pending proposals + decision endpoint.

Approving a proposal EXECUTES it server-side (audit Break 3/4 fix): the
phone only has to deliver the tap — risk re-check, broker order, bracket
legs, persistence, and the pending-state flip all happen here. The user can
approve from a push notification and put the phone away.

Decline stays a pure state write.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.approvals import (
    ApprovalProposalDto,
    DecisionRequest,
    DecisionResponse,
)
from app.services.broker_use import BrokerUnavailableError
from app.services.executor import (
    ExecutorError,
    ProposalNotFound,
    execute_proposal,
)
from app.services.store import get_store

logger = logging.getLogger("api.router.approvals")

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get(
    "/pending",
    response_model=list[ApprovalProposalDto],
    response_model_by_alias=True,
)
async def list_pending(
    user: AuthedUser = Depends(get_current_user),
) -> list[ApprovalProposalDto]:
    """Open proposals awaiting user decision. Auto-expired ones are filtered out."""
    _ = user
    store = get_store()
    return await store.list_pending()


@router.post(
    "/{proposal_id}/decision",
    response_model=DecisionResponse,
    response_model_by_alias=True,
)
async def decide(
    proposal_id: str,
    body: DecisionRequest,
    user: AuthedUser = Depends(get_current_user),
) -> DecisionResponse:
    """Approve (→ execute server-side) or decline a pending proposal.

    Approve outcomes:
      - executed=True + order        the deterministic chain passed; the
        order is at the broker (or paper-filled) with the chosen exit mode.
      - risk_blocked=True            the LAST-LINE risk re-check refused
        (world changed since drafting). The proposal STAYS PENDING so the
        user can retry once the condition clears.
      - executed=False + no block    no broker connection in live mode —
        the approval is recorded, nothing could execute.

    Idempotent: re-posting an approval for an already-executed proposal
    returns 404 (no longer pending), same as before.
    """
    store = get_store()

    if body.outcome == "declined":
        result = await store.decide(proposal_id, "declined")
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No pending proposal with id={proposal_id!r}",
            )
        return result

    # outcome == "approved" → the executor owns the whole transition:
    # risk re-check → broker order (+ bracket legs per exit_mode) →
    # order persistence → store.decide("approved", exit_mode=…).
    try:
        exec_result = await execute_proposal(
            user_id=user.id,
            proposal_id=proposal_id,
            exit_mode=body.exit_mode,
        )
    except ProposalNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except BrokerUnavailableError as exc:
        # Live mode with no usable broker connection. Record the approval
        # so the user's intent is on the books; surface why nothing ran.
        logger.info("approve without broker for %s — %s", proposal_id, exc)
        recorded = await store.decide(proposal_id, "approved", exit_mode=body.exit_mode)
        if recorded is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No pending proposal with id={proposal_id!r}",
            ) from exc
        return recorded.model_copy(
            update={"executed": False, "risk_reason": str(exc)}
        )
    except ExecutorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    if exec_result.risk_blocked:
        # Deliberately NOT decided — the proposal stays pending. The risk
        # picture can change (halt acknowledged, position closed) within
        # the proposal's market-day TTL.
        return DecisionResponse(
            proposal_id=proposal_id,
            outcome="approved",
            decided_at=datetime.now(UTC),
            executed=False,
            risk_blocked=True,
            risk_veto_rule=exec_result.risk_veto_rule,
            risk_reason=exec_result.risk_reason,
        )

    return DecisionResponse(
        proposal_id=proposal_id,
        outcome="approved",
        decided_at=datetime.now(UTC),
        executed=True,
        order=exec_result.order,
        risk_blocked=False,
        risk_reason=exec_result.risk_reason,
    )
