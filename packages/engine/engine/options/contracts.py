"""OCC symbol helpers + the ONE sanctioned constructor for an options
``RiskProposal``.

``engine.options.selection`` (contract/strike/expiry selection) and
``engine.options.sizing`` (premium-at-risk sizing) are a SEPARATE,
parallel track — not built here, and this module does not wait for them.
To keep this package independently mergeable, ``to_risk_proposal`` takes
an ALREADY-CONSTRUCTED ``OptionLegDetails`` plus explicit top-level
``RiskProposal`` scalar fields, rather than a new intermediate
"ContractSelection"-shaped type — whichever code builds the
``OptionLegDetails`` (today: this package's own tests and the executor's
execution-time re-risk-check; later: ``position_manager``'s options-close
branch, and eventually the council-side node-wiring code) only needs to
produce a value of a type that already exists in the shared foundation
(``engine.risk.types.OptionLegDetails``), sidestepping the need to
reconcile two independently-invented intermediate shapes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Literal

from broker.types import OccSymbol
from engine.options.selection import ContractQuote
from engine.risk.types import OptionLegDetails, RiskCaps, RiskProposal, Side

__all__ = [
    "OccSymbol",
    "contract_type_of",
    "fetch_option_candidates",
    "to_risk_proposal",
]


def contract_type_of(value: str) -> Literal["call", "put"]:
    """Narrow a plain string to the ``contract_type`` Literal
    ``OptionLegDetails`` requires.

    ``OccSymbol.contract_type`` (and any string a caller reads off a
    persisted proposal's JSONB) is typed as plain ``str`` — narrower at
    the type level than ``OptionLegDetails.contract_type`` needs, even
    though ``OccSymbol.parse`` only ever produces "call"/"put" at
    runtime. Defensive on the fallback: anything that isn't literally
    "put" is treated as "call" rather than raising, since a malformed
    contract_type here is exactly the kind of missing/bad data this
    package's rules already fail closed on elsewhere (illiquid_contract,
    iv_unavailable) — not a reason to crash the caller outright.
    """
    return "put" if value == "put" else "call"


def to_risk_proposal(
    *,
    symbol: str,
    side: Side,
    qty: int,
    estimated_notional: float,
    last_price: float,
    confidence: float,
    option: OptionLegDetails,
    closes_intraday_position: bool = False,
    is_intraday: bool = False,
) -> RiskProposal:
    """Build an options ``RiskProposal``. The ONE place ``is_option=True``
    and every ``option.*`` field get set.

    Centralizing this means the flag can't be forgotten ad hoc at one call
    site while another remembers it — exactly the kind of one-of-several-
    places-that-must-agree bug this repo's own rule-naming convention
    exists to make auditable, applied one level up at construction time.

    Takes ``side`` explicitly rather than deriving it from
    ``option.action``: ``engine.risk.types.Side`` only has BUY/SELL (see
    that module — the options-only BUY_TO_OPEN/SELL_TO_CLOSE values live
    on ``broker.types.Side`` instead, a deliberately separate enum for the
    broker wire boundary), so the caller decides the BUY/SELL direction
    and ``option.action`` carries the open/close nuance. This function
    does not cross-check that the two agree — ``naked_short_forbidden``
    validates ``option.action`` itself, unconditionally, independent of
    whatever ``side`` was passed. "Agents propose, deterministic code
    disposes" applies here too: this constructor proposes a shape, it
    does not dispose — the risk pipeline is what's allowed to veto.
    """
    return RiskProposal(
        symbol=symbol,
        side=side,
        qty=qty,
        estimated_notional=estimated_notional,
        last_price=last_price,
        confidence=confidence,
        is_option=True,
        option=option,
        closes_intraday_position=closes_intraday_position,
        is_intraday=is_intraday,
    )


async def fetch_option_candidates(
    underlying_symbol: str,
    *,
    api_key: str,
    secret_key: str,
    now: datetime,
    caps: RiskCaps | None = None,
) -> tuple[ContractQuote, ...]:
    """Chain snapshot + open-interest enrichment -> ``ContractQuote``
    candidates, ready for ``engine.options.selection.select_contract``.

    This is the "chain fetch + normalise" this module's own docstring
    promised (``docs/OPTIONS_PLAN.md`` §2.1) but never built until now —
    the real fetch lived, broken, directly in
    ``trading_agents.nodes.drafter`` instead (see that module's build-log
    history). Two calls, concurrently:

    - ``broker.alpaca.list_option_chain_quotes`` — bid/ask/delta/IV, from
      the correct chain-SNAPSHOT endpoint.
    - ``broker.alpaca.list_option_contracts`` (unchanged) — the ONLY
      source of ``open_interest``; the snapshot endpoint doesn't carry it
      at all. Merged in by exact OCC symbol string. This merge is
      necessary, not optional: ``engine.options.selection``'s
      ``_passes_liquidity`` hard-fails on ``open_interest is None`` (by
      design — "can't prove liquidity you can't see"), so leaving it
      unset would just relocate the chain-fetch bug this function
      replaces one stage downstream, under a different name.

    Windows both calls to ``RiskCaps.options_min_dte``/``options_max_dte``
    — the wide, AUTHORITATIVE bound re-checked independently at the risk
    gate — deliberately not ``selection.py``'s own narrower 21-45 DTE
    heuristic, which has its own documented reason not to import
    ``RiskCaps`` for its selection-only window. Fetching the wider range
    here means any current or future selection rule has what it needs.

    ``ContractQuote.volume`` is populated from ``ChainQuote.last_trade_size``
    — the size of the single last trade, not cumulative daily volume; no
    field on either Alpaca endpoint reports the latter. A real but
    documented-imperfect liquidity proxy (a single large print can outrank
    many small ones) — true daily volume would need a third, per-contract
    call to the bars endpoint, which is out of scope here.

    Does not itself catch a broker-layer failure — an exception from
    either call propagates uncaught (the caller, ``trading_agents.nodes.
    drafter._fetch_option_candidates``, is what degrades this to a HOLD).
    """
    resolved_caps = caps or RiskCaps.from_env()
    gte = now.date() + timedelta(days=resolved_caps.options_min_dte)
    lte = now.date() + timedelta(days=resolved_caps.options_max_dte)

    from broker.alpaca import list_option_chain_quotes, list_option_contracts

    chain_quotes, contracts = await asyncio.gather(
        list_option_chain_quotes(
            underlying_symbol,
            api_key=api_key,
            secret_key=secret_key,
            expiration_date_gte=gte,
            expiration_date_lte=lte,
        ),
        list_option_contracts(
            [underlying_symbol],
            api_key=api_key,
            secret_key=secret_key,
            expiration_date_gte=gte,
            expiration_date_lte=lte,
        ),
    )

    oi_by_symbol: dict[str, int] = {}
    for contract in contracts:
        if contract.open_interest is None:
            continue
        try:
            oi_by_symbol[contract.symbol] = int(contract.open_interest)
        except (TypeError, ValueError):
            continue

    return tuple(
        ContractQuote(
            occ_symbol=q.occ_symbol,
            contract_type=contract_type_of(q.contract_type),
            strike=q.strike,
            expiry=q.expiry,
            bid=q.bid,
            ask=q.ask,
            open_interest=oi_by_symbol.get(q.occ_symbol),
            volume=int(q.last_trade_size) if q.last_trade_size is not None else None,
            delta=q.delta,
            implied_volatility=q.implied_volatility,
            quote_ts=getattr(q, "quote_ts", None),
        )
        for q in chain_quotes
    )
