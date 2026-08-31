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
``get_entry_thesis``) are added to this module by the workstream that also
owns their handlers (``readonly.py``/``registry.py``), so schema and handler
land together.
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
