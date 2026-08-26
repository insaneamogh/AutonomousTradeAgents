"""Alpaca corporate actions (``/v1/corporate-actions``) → an ``events`` block.

An ex-dividend date or a split inside the holding horizon changes the
trade, and both are knowable in advance for free:

  - **Ex-dividend.** The stock opens lower by roughly the dividend on the
    ex-date. On a long that is neutral (you receive the dividend), but a
    2%-ATR name with a 1% dividend has an ex-date gap the ATR stop cannot
    tell apart from a real breakdown. On a SHORT it is not neutral at all:
    the short seller PAYS the dividend to the lender. That is a known,
    dated, unavoidable cost the sizer does not model, which makes it worth
    surfacing loudly before a short is opened over an ex-date.
  - **Splits.** A forward split rewrites every price level the strategy is
    reasoning about. The bars provider back-adjusts history, but a split
    landing *inside* an open position is a live event.

**Earnings are deliberately absent, and that is a data-source limit, not
an omission.** Alpaca's corporate-actions API covers distributions and
capital changes — cash dividends, stock dividends, splits, reverse splits,
mergers, spinoffs, redemptions. It does not publish an earnings calendar,
and the deprecated ``/v2/corporate_actions/announcements`` endpoint does
not either. An earnings-blackout flag is genuinely worth having (an
earnings print inside a 5-day hold is a different trade, and the single
biggest source of overnight gap risk on a short) but it needs a vendor we
do not have. Wiring a fabricated or scraped earnings date into a risk flag
would be worse than not having one, so this module reports what Alpaca
actually knows and ``earnings_date_known`` stays False.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("engine.features.corporate_actions")

LOOKAHEAD_DAYS = 30
"""How far forward to ask. Ex-dates are announced weeks ahead; 30 days
comfortably covers the longest (20-day) time stop this system uses."""


@dataclass(frozen=True)
class CorporateEvent:
    """One dated corporate action. ``kind`` is a named identifier."""

    kind: str
    """``cash_dividend`` | ``stock_dividend`` | ``forward_split`` |
    ``reverse_split`` | ``merger`` | ``spinoff`` | ``other``."""
    effective_date: date
    detail: str = ""
    rate: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "date": self.effective_date.isoformat(),
            "detail": self.detail,
            "rate": self.rate,
        }


@dataclass(frozen=True)
class CorporateActionFeatures:
    """Forward-looking corporate-action flags for one symbol + horizon."""

    ex_dividend_in_horizon: bool | None = None
    days_to_ex_dividend: int | None = None
    dividend_rate: float | None = None
    dividend_yield_of_move_pct: float | None = None
    """The dividend as a % of last price — i.e. the size of the ex-date gap
    in the same units the ATR stop is measured in."""
    split_in_horizon: bool | None = None
    corporate_event_in_horizon: bool | None = None
    earnings_date_known: bool = False
    """Always False: Alpaca publishes no earnings calendar. Present so a
    prompt can say "unknown" rather than silently implying "none"."""
    upcoming: tuple[CorporateEvent, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "upcoming"}
        d["upcoming"] = [e.as_dict() for e in self.upcoming]
        return d


def compute_corporate_actions(
    events: list[CorporateEvent],
    *,
    horizon_days: int,
    last_price: float | None = None,
    today: date | None = None,
) -> CorporateActionFeatures:
    """Reduce dated events to horizon flags. Pure — no network.

    ``horizon_days`` is the trade's time stop, so "in horizon" means "this
    lands while we would still be holding".
    """
    at = today or datetime.now(UTC).date()
    end = at + timedelta(days=max(0, horizon_days))
    upcoming = sorted(
        (e for e in events if at <= e.effective_date <= end),
        key=lambda e: e.effective_date,
    )

    div = next((e for e in upcoming if e.kind in ("cash_dividend", "stock_dividend")), None)
    split = next((e for e in upcoming if e.kind in ("forward_split", "reverse_split")), None)

    gap_pct: float | None = None
    if div is not None and div.rate and last_price and last_price > 0:
        gap_pct = round((div.rate / last_price) * 100.0, 3)

    return CorporateActionFeatures(
        ex_dividend_in_horizon=div is not None,
        days_to_ex_dividend=(div.effective_date - at).days if div else None,
        dividend_rate=div.rate if div else None,
        dividend_yield_of_move_pct=gap_pct,
        split_in_horizon=split is not None,
        corporate_event_in_horizon=bool(upcoming),
        upcoming=tuple(upcoming),
    )


@runtime_checkable
class CorporateActionsProvider(Protocol):
    name: str

    async def fetch(self, symbol: str) -> list[CorporateEvent]: ...


_KIND_BY_KEY: dict[str, str] = {
    "cash_dividends": "cash_dividend",
    "stock_dividends": "stock_dividend",
    "forward_splits": "forward_split",
    "reverse_splits": "reverse_split",
    "unit_splits": "forward_split",
    "mergers": "merger",
    "spinoffs": "spinoff",
    "redemptions": "other",
    "name_changes": "other",
    "worthless_removals": "other",
}

_DATE_KEYS = ("ex_date", "process_date", "payable_date", "effective_date", "record_date")


class AlpacaCorporateActionsProvider:
    """``/v1/corporate-actions`` over plain httpx.

    alpaca-py's typed wrapper covers the deprecated announcements endpoint,
    not this one, so we call the documented REST route directly — one GET,
    no SDK surface to fight.
    """

    name = "alpaca-corporate-actions"

    _BASE = "https://data.alpaca.markets/v1/corporate-actions"

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self._cache: dict[tuple[str, date], list[CorporateEvent]] = {}

    async def fetch(self, symbol: str) -> list[CorporateEvent]:
        sym = symbol.upper()
        today = datetime.now(UTC).date()
        cached = self._cache.get((sym, today))
        if cached is not None:
            return cached

        import httpx

        params = {
            "symbols": sym,
            "start": today.isoformat(),
            "end": (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat(),
            "limit": 100,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self._BASE, headers=self._headers, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception:
            logger.exception("corporate actions: fetch failed for %s", sym)
            return []

        events = parse_corporate_actions(payload, symbol=sym)
        self._cache[(sym, today)] = events
        return events


def parse_corporate_actions(payload: dict[str, Any], *, symbol: str) -> list[CorporateEvent]:
    """Map the API's ``{corporate_actions: {kind: [row, ...]}}`` shape to events.

    Split out from the fetch so the parser is testable against a captured
    response body without a network call — the shape is the part that
    breaks when the vendor changes something.
    """
    blocks = (payload or {}).get("corporate_actions") or {}
    out: list[CorporateEvent] = []
    for key, rows in blocks.items():
        kind = _KIND_BY_KEY.get(key, "other")
        for row in rows or []:
            if str(row.get("symbol", "")).upper() not in (symbol.upper(), ""):
                continue
            when = _first_date(row)
            if when is None:
                continue
            rate = row.get("rate")
            detail = key.replace("_", " ").rstrip("s")
            if kind == "cash_dividend" and rate is not None:
                detail = f"cash dividend ${float(rate):.4f}"
                if row.get("special"):
                    detail += " (special)"
            out.append(
                CorporateEvent(
                    kind=kind,
                    effective_date=when,
                    detail=detail,
                    rate=float(rate) if rate is not None else None,
                )
            )
    return sorted(out, key=lambda e: e.effective_date)


def _first_date(row: dict[str, Any]) -> date | None:
    for key in _DATE_KEYS:
        raw = row.get(key)
        if raw:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
    return None


def corporate_actions_provider_from_env() -> AlpacaCorporateActionsProvider | None:
    """Real provider when Alpaca data keys are set; otherwise None."""
    import os

    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret:
        return None
    return AlpacaCorporateActionsProvider(api_key, secret)
