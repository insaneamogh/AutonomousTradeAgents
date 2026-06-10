"""Static asset metadata — symbol → sector / correlation cluster.

Phase 0/1: small hand-curated dict, enough to exercise sector + correlation
rules in unit tests. Phase 2 replaces this with a ``sectors`` table populated
by the daily ingest job (FMP/yfinance/Polygon).
"""

from __future__ import annotations


# Coarse sector buckets matching the GICS L1 names — close enough for
# concentration math. Anything not in the map falls into "other".
SECTOR_BY_SYMBOL: dict[str, str] = {
    # Information Technology
    "AAPL": "tech",   "MSFT": "tech",   "GOOGL": "tech",  "GOOG": "tech",
    "META": "tech",   "AMZN": "tech",   "NVDA": "tech",   "AMD": "tech",
    "AVGO": "tech",   "ORCL": "tech",   "ADBE": "tech",   "CRM": "tech",
    "INTC": "tech",   "CSCO": "tech",   "QCOM": "tech",
    # Financials — banks + brokers cluster heavily on rates
    "JPM": "financials", "BAC": "financials", "WFC": "financials",
    "GS": "financials",  "MS": "financials",  "C": "financials",
    "SCHW": "financials",
    # Energy
    "XOM": "energy", "CVX": "energy", "COP": "energy", "SLB": "energy",
    # Healthcare
    "JNJ": "healthcare", "UNH": "healthcare", "PFE": "healthcare",
    "MRK": "healthcare", "ABBV": "healthcare", "LLY": "healthcare",
    # Consumer Discretionary
    "TSLA": "consumer_disc", "HD": "consumer_disc", "MCD": "consumer_disc",
    "NKE": "consumer_disc",  "SBUX": "consumer_disc",
    # ETFs — buckets by exposure
    "SPY": "etf_broad",  "QQQ": "etf_broad",  "IWM": "etf_broad",
    "XLK": "etf_tech",   "XLF": "etf_financials",
}

# Correlation clusters — same-cluster positions tend to move together.
# Phase 0/1 uses these as a coarse correlation cap; Phase 2 swaps in
# a real ρ matrix from historical returns.
CLUSTER_BY_SYMBOL: dict[str, str] = {
    # Mega-cap tech moves together
    "AAPL": "megacap_tech", "MSFT": "megacap_tech", "GOOGL": "megacap_tech",
    "GOOG": "megacap_tech", "META": "megacap_tech", "AMZN": "megacap_tech",
    # AI-capex
    "NVDA": "ai_capex", "AMD": "ai_capex", "AVGO": "ai_capex",
    # Money-center banks
    "JPM": "money_center_banks", "BAC": "money_center_banks",
    "WFC": "money_center_banks", "C": "money_center_banks",
    # Oil majors
    "XOM": "oil_majors", "CVX": "oil_majors", "COP": "oil_majors",
}


def sector_for(symbol: str) -> str:
    return SECTOR_BY_SYMBOL.get(symbol.upper(), "other")


def cluster_for(symbol: str) -> str | None:
    return CLUSTER_BY_SYMBOL.get(symbol.upper())
