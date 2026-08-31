"""Anthropic tool-use JSON schemas for the options agents.

Frozen contract, per ``docs/IMPL_OPTIONS_AGENTS.md`` §1 — ``guard.py`` and
``trade.py`` (IMPL 2-guard) are built against these exact field names, and
the Bull/Bear prompts (IMPL 2-agents) are written assuming this exact shape.
Do not add fields here without updating the guard's ``before()`` validation
and both consuming workstreams.

The two mutating tools deliberately do NOT accept a contract, strike, expiry
or quantity. ``select_contract`` + ``options_position_size`` derive those
deterministically inside the guard — so a hallucinated OCC symbol is not a
category of bug that can exist here, because the agent never supplies one.

The six read-only tool schemas (``get_funnel_counts``, ``get_option_snapshot``,
``get_underlying_bars``, ``get_iv_rank``, ``get_position_snapshot``,
``get_entry_thesis``) live below, next to their handlers
(``tools/readonly.py``) and registration (``tools/registry.py``), so schema
and handler land together.
"""

from __future__ import annotations

from typing import Any

OPEN_OPTION_TRADE: dict[str, Any] = {
    "name": "open_option_trade",
    "description": (
        "Open a long option position on an underlying. You do NOT choose the "
        "contract, strike, expiry or quantity — the deterministic selector picks "
        "them from your direction and conviction. The trade is placed only if it "
        "clears all 13 risk rules; if it does not you will be told which rule "
        "refused it and may adjust once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying": {
                "type": "string",
                "description": "Ticker, e.g. NVDA. Never an OCC symbol.",
            },
            "direction": {"type": "string", "enum": ["long", "short"]},
            "strategy": {
                "type": "string",
                "description": "A registered strategy id.",
            },
            "conviction": {"type": "number", "minimum": 0, "maximum": 1},
            "thesis": {
                "type": "string",
                "description": "Must state a timeframe.",
            },
            "take_profit_pct": {"type": "number", "minimum": 40, "maximum": 300},
            "stop_loss_pct": {"type": "number", "minimum": 25, "maximum": 50},
        },
        "required": [
            "underlying",
            "direction",
            "strategy",
            "conviction",
            "thesis",
            "take_profit_pct",
            "stop_loss_pct",
        ],
    },
}

ADJUST_OPTION_POSITION: dict[str, Any] = {
    "name": "adjust_option_position",
    "description": (
        "Act on an open option position whose trailing ratchet reported a "
        "material change. Stops and take-profits may only move UP (tighter/"
        "higher). Any request to loosen protection is refused."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision_id": {"type": "string"},
            "action": {
                "type": "string",
                "enum": [
                    "SCALE_IN",
                    "EXIT_NOW",
                    "RAISE_TAKE_PROFIT",
                    "TIGHTEN_STOP",
                    "HOLD",
                ],
            },
            "value": {
                "type": "number",
                "description": "New pct for RAISE/TIGHTEN.",
            },
            "reason": {"type": "string"},
        },
        "required": ["decision_id", "action", "reason"],
    },
}


# ─────────────────────────────────────────────────────────────────────
# Read-only tools — handlers in ``tools/readonly.py``, registered in
# ``tools/registry.py``. None of these accept a ``user_id``/tenant field:
# the guard injects the caller's own ``user_id`` from ``GuardContext`` at
# dispatch time (see ``tools/readonly.py`` module docstring) — a schema
# field here would let the model ask for another tenant's data instead.
# ─────────────────────────────────────────────────────────────────────

GET_FUNNEL_COUNTS: dict[str, Any] = {
    "name": "get_funnel_counts",
    "description": (
        "The contract-selection funnel for the most recent council pass(es) "
        "on one underlying: how many candidate contracts survived each of "
        "the six selection stages (contract type, DTE window, delta band, "
        "liquidity, IV present, IV-vs-realized-vol band), and the named "
        "reason the funnel emptied, if it did. The current pass's own "
        "pre-pass context usually already includes this — call this tool "
        "to check a PRIOR pass on the same underlying."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying": {
                "type": "string",
                "description": "Ticker, e.g. NVDA. Never an OCC symbol.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "How many recent passes to return. Default 1 (most recent).",
            },
        },
        "required": ["underlying"],
    },
}

GET_OPTION_SNAPSHOT: dict[str, Any] = {
    "name": "get_option_snapshot",
    "description": (
        "Live bid/ask/greeks/IV for one underlying's option chain. Pass "
        "occ_symbol to look up one specific contract (e.g. one from an open "
        "position); omit it to get the most liquid candidates across the "
        "chain. Volume is the last trade's size, not daily volume — a real "
        "but imperfect liquidity proxy; open interest is the reliable one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying": {
                "type": "string",
                "description": "Ticker, e.g. NVDA. Never an OCC symbol.",
            },
            "occ_symbol": {
                "type": "string",
                "description": "Optional specific contract, e.g. NVDA260918C00250000.",
            },
        },
        "required": ["underlying"],
    },
}

GET_UNDERLYING_BARS: dict[str, Any] = {
    "name": "get_underlying_bars",
    "description": (
        "Recent daily closes and volume for the underlying stock — a quick "
        "read on trend, not a substitute for the technical analyst's full "
        "feature set."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying": {"type": "string", "description": "Ticker, e.g. NVDA."},
            "lookback_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 90,
                "description": "Calendar days of history. Default 30.",
            },
        },
        "required": ["underlying"],
    },
}

GET_IV_RANK: dict[str, Any] = {
    "name": "get_iv_rank",
    "description": (
        "Where the underlying's at-the-money implied volatility sits "
        "relative to what this SAME running system has itself observed — "
        "0 is the lowest IV seen, 100 the highest. Not a vendor IV rank: "
        "this codebase has no persisted IV history, so the lookback is only "
        "as deep as this process's own accumulated samples. Returns "
        "iv_rank: null with a named reason when there isn't enough history "
        "yet — never a fabricated number."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying": {"type": "string", "description": "Ticker, e.g. NVDA."},
        },
        "required": ["underlying"],
    },
}

GET_POSITION_SNAPSHOT: dict[str, Any] = {
    "name": "get_position_snapshot",
    "description": (
        "Current state of one open option position: entry premium, current "
        "P&L%, the trailing ratchet's peak and trail line, DTE, and days "
        "held. Use the decision_id from the position you were told about — "
        "this tool does not search, it reads one position back."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision_id": {"type": "string"},
        },
        "required": ["decision_id"],
    },
}

GET_ENTRY_THESIS: dict[str, Any] = {
    "name": "get_entry_thesis",
    "description": (
        "The original thesis text (and bull/bear cases) recorded when this "
        "position was opened, plus a best-effort parsed deadline and "
        "whether it has passed. A thesis with no parseable timeframe "
        "returns a null deadline rather than a guess."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision_id": {"type": "string"},
        },
        "required": ["decision_id"],
    },
}

READ_ONLY_TOOLS: tuple[dict[str, Any], ...] = (
    GET_FUNNEL_COUNTS,
    GET_OPTION_SNAPSHOT,
    GET_UNDERLYING_BARS,
    GET_IV_RANK,
    GET_POSITION_SNAPSHOT,
    GET_ENTRY_THESIS,
)
"""Convenience aggregate for whoever wires the Bull/Bear agents' tool list
(``options/agents.py``, a parallel workstream) — both agents get all six;
only the resolved Bull additionally gets ``OPEN_OPTION_TRADE``."""
