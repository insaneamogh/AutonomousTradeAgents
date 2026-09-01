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
    # Broad-market index ETFs.
    #
    # These were in SECTOR_BY_SYMBOL ("etf_broad") but in NO cluster, so
    # `max_correlation_cluster` could not see index exposure at all. On
    # 2026-08-31 the desk opened six long calls inside ten minutes —
    # NVDA x3 (ai_capex), SPY x2 (no cluster), QQQ x1 (no cluster) — which
    # the cap read as ONE clustered name against a limit of four. It was
    # one levered long-beta bet wearing six position rows, and it gapped
    # down together the next morning for -3.67% of equity before a single
    # stop could fire.
    #
    # One cluster for all of them, not one per index: SPY/QQQ/DIA/IWM
    # differ in composition but not in what actually hurts a long-only
    # book — they are the same beta. Splitting them would let a third
    # index long through on a technicality, which is exactly the failure
    # being fixed.
    "SPY": "broad_market", "QQQ": "broad_market",
    "DIA": "broad_market", "IWM": "broad_market",
    "VOO": "broad_market", "VTI": "broad_market",
}


def sector_for(symbol: str) -> str:
    return SECTOR_BY_SYMBOL.get(symbol.upper(), "other")


def cluster_for(symbol: str) -> str | None:
    return CLUSTER_BY_SYMBOL.get(symbol.upper())
