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

from typing import Literal

from broker.types import OccSymbol
from engine.risk.types import OptionLegDetails, RiskProposal, Side

__all__ = ["OccSymbol", "contract_type_of", "to_risk_proposal"]


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
