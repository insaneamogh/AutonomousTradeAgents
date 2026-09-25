"""Market detection from exchange-qualified symbols.

Zerodha symbols arrive as ``EXCHANGE:TRADINGSYMBOL`` ("NSE:RELIANCE",
"NFO:NIFTY24DECFUT"); Alpaca symbols are bare ("AAPL"). The risk engine
keys market-specific rules (PDT, wash-sale, lot sizes, MIS square-off)
off these pure helpers so no rule re-implements symbol parsing.

Anything without a recognized Indian exchange prefix is treated as US —
that matches the broker layer, where bare symbols route to Alpaca.
"""

from __future__ import annotations

INDIA_EXCHANGES: frozenset[str] = frozenset(
    {"NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"}
)
INDIA_DERIVATIVE_EXCHANGES: frozenset[str] = frozenset(
    {"NFO", "BFO", "MCX", "CDS", "BCD"}
)


def exchange_of(symbol: str) -> str | None:
    """Return the upper-cased exchange prefix of ``EXCHANGE:SYMBOL``, or None for bare symbols."""
    prefix, sep, _ = symbol.partition(":")
    return prefix.strip().upper() if sep else None


def market_of(symbol: str) -> str:
    """Classify a symbol as ``"IN"`` (Indian exchange prefix) or ``"US"`` (everything else)."""
    return "IN" if exchange_of(symbol) in INDIA_EXCHANGES else "US"


def is_derivative(symbol: str) -> bool:
    """True when the symbol routes to an Indian derivatives segment (NFO/BFO/MCX/CDS/BCD)."""
    return exchange_of(symbol) in INDIA_DERIVATIVE_EXCHANGES


def tradingsymbol_of(symbol: str) -> str:
    """Return the tradingsymbol with any exchange prefix stripped."""
    _, sep, rest = symbol.partition(":")
    return rest if sep else symbol


# Which broker trades which market. One place, so no loop hardcodes a
# broker name again (docs/PLAN_ZERODHA.md Z1).
BROKER_FOR_MARKET: dict[str, str] = {"US": "alpaca", "IN": "zerodha"}
MARKET_FOR_BROKER: dict[str, str] = {b: m for m, b in BROKER_FOR_MARKET.items()}


def broker_for_symbol(symbol: str) -> str:
    """"zerodha" for an Indian-exchange symbol, "alpaca" otherwise."""
    return BROKER_FOR_MARKET[market_of(symbol)]


def market_for_broker(broker: str) -> str:
    """"IN" for zerodha, "US" for alpaca (and for anything unknown)."""
    return MARKET_FOR_BROKER.get(str(broker).lower(), "US")
