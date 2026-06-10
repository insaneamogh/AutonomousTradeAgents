"""/api/v1/approvals — pending proposals + decision endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.approvals import (
    ApprovalProposalDto,
    DecisionRequest,
    DecisionResponse,
)
from app.services.store import get_store

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
    """Approve or decline a pending proposal. Idempotent on (proposal_id,
    outcome) — re-posting the same decision returns the original record.
    """
    _ = user
    store = get_store()
    result = await store.decide(proposal_id, body.outcome)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending proposal with id={proposal_id!r}",
        )
    return result
