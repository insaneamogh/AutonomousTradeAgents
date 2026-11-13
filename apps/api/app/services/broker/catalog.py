"""Static description of the brokers the app knows about.

Display/discovery only. ``broker_use.SUPPORTED_BROKERS`` stays the
authoritative list of brokers the backend can actually trade through.
``missing_config`` carries env var NAMES only, never values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from app.services.broker import alpaca_oauth, zerodha_connect
from app.services.broker.env_bootstrap import env_keys_present

AuthKind = Literal["oauth", "request_token", "api_key"]
CatalogStatus = Literal["available", "needs_server_config", "planned"]


@dataclass(frozen=True)
class BrokerCatalogEntry:
    id: str
    name: str
    market: Literal["US", "IN"]
    currency: Literal["USD", "INR"]
    auth: AuthKind
    status: CatalogStatus
    supports_paper: bool
    supports_options: bool
    note: str
    missing_config: tuple[str, ...] = ()


# broker id -> (market, currency); used by the connection response too.
BROKER_MARKETS: dict[str, tuple[str, str]] = {
    "alpaca": ("US", "USD"),
    "zerodha": ("IN", "INR"),
    "groww": ("IN", "INR"),
    "indmoney": ("IN", "INR"),
}


def market_for(broker: str) -> tuple[str, str]:
    return BROKER_MARKETS.get(broker.lower(), ("US", "USD"))


def _unset(*names: str) -> tuple[str, ...]:
    return tuple(n for n in names if not os.environ.get(n, "").strip())


def build_catalog() -> list[BrokerCatalogEntry]:
    alpaca_ok = alpaca_oauth.is_configured() or env_keys_present()
    alpaca_missing = (
        () if alpaca_ok
        else ("ALPACA_OAUTH_CLIENT_ID", "ALPACA_OAUTH_CLIENT_SECRET")
    )
    zerodha_ok = zerodha_connect.is_configured()
    zerodha_missing = () if zerodha_ok else _unset("KITE_API_KEY", "KITE_API_SECRET")
    return [
        BrokerCatalogEntry(
            id="alpaca", name="Alpaca", market="US", currency="USD",
            auth="oauth",
            status="available" if alpaca_ok else "needs_server_config",
            supports_paper=True, supports_options=True,
            note="US stocks and options. Paper and live accounts.",
            missing_config=alpaca_missing,
        ),
        BrokerCatalogEntry(
            id="zerodha", name="Zerodha (Kite)", market="IN", currency="INR",
            auth="request_token",
            status="available" if zerodha_ok else "needs_server_config",
            supports_paper=False, supports_options=True,
            note=(
                "Kite session expires daily around 06:00 IST; "
                "API orders need a registered static IP"
            ),
            missing_config=zerodha_missing,
        ),
        BrokerCatalogEntry(
            id="groww", name="Groww", market="IN", currency="INR",
            auth="api_key", status="planned",
            supports_paper=False, supports_options=False,
            note=(
                "Official Trade API exists; each user supplies their own API "
                "key and approves it daily. Adapter not built."
            ),
        ),
        BrokerCatalogEntry(
            id="indmoney", name="INDmoney", market="IN", currency="INR",
            auth="api_key", status="planned",
            supports_paper=False, supports_options=False,
            note="INDstocks API exists; per-user access token. Adapter not built.",
        ),
    ]
