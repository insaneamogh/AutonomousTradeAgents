"""/api/v1/circuit-breaker — drawdown halt status + acknowledgement.

GET  /api/v1/circuit-breaker             is the caller currently halted?
POST /api/v1/circuit-breaker/acknowledge user clears the halt (resume)

Backs DESIGN.md's persistent danger banner: the mobile app polls the GET
and shows the banner while ``halted``; the POST is the explicit
acknowledgement that flips the breaker to manual_override so trading
resumes. Acknowledge requires a real session (never the dev bypass) —
it's a state change that unblocks real orders.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.middleware.auth import AuthedUser, get_current_user, require_real_auth
from app.schemas.circuit_breaker import CircuitBreakerResponse
from app.services.platform import circuit_breaker_service as cb

router = APIRouter(prefix="/circuit-breaker", tags=["circuit-breaker"])


def _to_response(s: cb.CircuitBreakerStatus) -> CircuitBreakerResponse:
    return CircuitBreakerResponse(
        halted=s.halted,
        reason=s.reason,
        halted_at=s.halted_at,
        observed_drawdown_pct=s.observed_drawdown_pct,
        threshold_pct=s.threshold_pct,
    )


@router.get("", response_model=CircuitBreakerResponse, response_model_by_alias=True)
async def status(user: AuthedUser = Depends(get_current_user)) -> CircuitBreakerResponse:
    return _to_response(await cb.get_status(user.id))


@router.post(
    "/acknowledge",
    response_model=CircuitBreakerResponse,
    response_model_by_alias=True,
)
async def acknowledge(
    user: AuthedUser = Depends(require_real_auth),
) -> CircuitBreakerResponse:
    return _to_response(await cb.acknowledge(user.id))
