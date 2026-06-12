"""/api/v1/watchlist — the symbols the agent tracks for this user.

The daily council iterates this list (falling back to the default
watchlist when a user hasn't curated one). v1 accepts US stocks + ETF
tickers only — options/futures were explicitly cut from scope, and
futures don't exist on Alpaca anyway.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.watchlist import AddWatchlistRequest, WatchlistItemDto
from app.services.watchlist_store import SYMBOL_RE, get_watchlist_store

logger = logging.getLogger("api.router.watchlist")

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

MAX_WATCHLIST_SIZE = 30


def _to_dto(item) -> WatchlistItemDto:
    return WatchlistItemDto(
        id=item.id,
        symbol=item.symbol,
        asset_class="equity",
        active=item.active,
        created_at=item.created_at,
    )


@router.get("", response_model=list[WatchlistItemDto], response_model_by_alias=True)
async def list_watchlist(
    user: AuthedUser = Depends(get_current_user),
) -> list[WatchlistItemDto]:
    items = await get_watchlist_store().list_items(user.id)
    return [_to_dto(i) for i in items]


@router.post(
    "",
    response_model=WatchlistItemDto,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def add_symbol(
    body: AddWatchlistRequest,
    user: AuthedUser = Depends(get_current_user),
) -> WatchlistItemDto:
    symbol = body.symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{symbol!r} is not a valid US equity/ETF ticker. "
                "v1 tracks stocks and ETFs only — options and futures are out of scope."
            ),
        )

    store = get_watchlist_store()
    existing = await store.list_items(user.id)
    if len(existing) >= MAX_WATCHLIST_SIZE and symbol not in {i.symbol for i in existing}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Watchlist is capped at {MAX_WATCHLIST_SIZE} symbols — every name "
                "costs a daily council run. Remove one first."
            ),
        )

    item = await store.add(user.id, symbol)
    logger.info("watchlist: %s added %s", user.id, symbol)
    return _to_dto(item)


@router.delete("/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_symbol(
    symbol: str,
    user: AuthedUser = Depends(get_current_user),
) -> None:
    removed = await get_watchlist_store().remove(user.id, symbol.strip().upper())
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{symbol.upper()!r} is not on the watchlist.",
        )
