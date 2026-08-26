"""/api/v1/scanner/status — deterministic-scanner / trigger-loop status.

Distinct from ``GET /health/full``: that endpoint is a lossy one-line
summary per component and can't carry a signal list. This endpoint's
payload is the scanner's actual last-scan detail — which named rule fired,
on which symbol, with what strength/detail — so it gets its own route
rather than growing ``HealthResponse``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.scanner import ScannerStatusResponse
from app.services.council.scanner_status import build_scanner_status_report

router = APIRouter(prefix="/scanner", tags=["scanner"])


@router.get(
    "/status",
    response_model=ScannerStatusResponse,
    response_model_by_alias=True,
)
async def scanner_status(
    user: AuthedUser = Depends(get_current_user),
) -> ScannerStatusResponse:
    """Trigger-loop status: armed state, last scan, last signals.

    Uses ``get_current_user`` (NOT ``require_real_auth``) so this still
    renders under ``DEV_AUTH_BYPASS=1`` for local demo use — same
    reasoning as ``health.py``. The report itself isn't per-user (the
    scanner watches one configured watchlist, not a per-user one), so
    ``user`` only gates the route.
    """
    del user
    return await build_scanner_status_report()
