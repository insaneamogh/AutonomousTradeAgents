"""/api/v1/orders — execute an approved proposal.

POST /api/v1/orders/execute/{proposal_id}
    Re-evaluates risk against the latest account state, places the order
    via the user's Alpaca connection, persists the order row, and returns
    the camelCase OrderResponse.

Same-user check is implicit: ``with_broker_client(user_id)`` only opens
the caller's own connection, so even if a malicious client guesses
another user's proposal_id they can't execute it through their own
broker session.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.middleware.auth import AuthedUser, require_real_auth
from app.schemas.orders import ExecuteResponse
from app.services.broker_use import BrokerUnavailableError
from app.services.crypto import is_available as crypto_available
from app.services.executor import (
    ExecutorError,
    ProposalNotFound,
    execute_proposal,
)
from app.services.paper_broker import trading_mode

logger = logging.getLogger("api.router.orders")

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "/execute/{proposal_id}",
    response_model=ExecuteResponse,
    response_model_by_alias=True,
)
async def execute(
    proposal_id: str,
    user: AuthedUser = Depends(require_real_auth),
) -> ExecuteResponse:
    # Paper mode never decrypts a broker token — crypto isn't needed.
    if trading_mode() != "paper" and not crypto_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Order execution requires the 'cryptography' Python package "
                "(needed to decrypt the broker token). Run `uv sync`."
            ),
        )

    try:
        return await execute_proposal(user_id=user.id, proposal_id=proposal_id)
    except ProposalNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    except BrokerUnavailableError as exc:
        # 412 Precondition Failed — the user needs to connect Alpaca first.
        # 503 if it's the crypto-not-installed case (we already returned
        # that above, but the executor may also raise it on decrypt fail).
        msg = str(exc)
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if "uv sync" in msg
            else status.HTTP_412_PRECONDITION_FAILED
        )
        raise HTTPException(status_code=code, detail=msg) from exc
    except ExecutorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Last-resort guard. Don't leak the underlying error to the
        # client (could contain SDK error messages); log it locally.
        logger.exception("executor: unhandled error for proposal=%s", proposal_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="broker call failed — see server logs",
        ) from exc
