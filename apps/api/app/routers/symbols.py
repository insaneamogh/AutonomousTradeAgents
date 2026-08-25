"""/api/v1/symbols — ticker typeahead.

Backs the Run box's search field so the user picks a real, tradable
symbol instead of typing free text. Reading the broker's own universe
means the list is exactly what we can trade — no third-party symbol
feed to drift out of sync with what Alpaca will actually accept.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.base import CamelCaseModel
from app.services.broker.symbol_search import search_symbols

logger = logging.getLogger("api.router.symbols")

router = APIRouter(prefix="/symbols", tags=["symbols"])


class SymbolHitDto(CamelCaseModel):
    symbol: str
    name: str
    fractionable: bool


@router.get("/search", response_model=list[SymbolHitDto], response_model_by_alias=True)
async def search(
    q: str = Query(min_length=1, max_length=40, description="Ticker or company name"),
    limit: int = Query(default=10, ge=1, le=25),
    user: AuthedUser = Depends(get_current_user),
) -> list[SymbolHitDto]:
    """Ranked tradable symbols matching ``q``.

    Empty when the deployment has no Alpaca data keys — the client
    falls back to free-text entry, which the run endpoint still
    validates before spending anything.
    """
    _ = user
    hits = await search_symbols(q, limit=limit)
    return [
        SymbolHitDto(symbol=h.symbol, name=h.name, fractionable=h.fractionable)
        for h in hits
    ]
