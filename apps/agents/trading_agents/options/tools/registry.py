"""Tool name -> handler. A flat dict, deliberately kept dead simple —
``guard.dispatch_tool_call`` just does ``REGISTRY.get(call.name)``.

Every handler shares the same shape: ``(args, ctx, guard_payload) -> dict``.
See ``guard.dispatch_tool_call`` for why the third argument exists, and
``tools/readonly.py``'s module docstring for why the six read-only
handlers accept-and-ignore it via a default rather than requiring it.

Eight entries: the two mutating tools (``tools/trade.py``, gated by the
guard's full 12-step stack / ratchet invariant) and the six read-only
tools (``tools/readonly.py``, allowed through unconditionally by
``ToolGuard.before()``'s read-only branch).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trading_agents.options.tools.guard import GuardContext
from trading_agents.options.tools.readonly import (
    get_entry_thesis,
    get_funnel_counts,
    get_iv_rank,
    get_option_snapshot,
    get_position_snapshot,
    get_underlying_bars,
)
from trading_agents.options.tools.trade import adjust_option_position, open_option_trade

Handler = Callable[[dict[str, Any], GuardContext, dict[str, Any]], Awaitable[dict[str, Any]]]

REGISTRY: dict[str, Handler] = {
    "open_option_trade": open_option_trade,
    "adjust_option_position": adjust_option_position,
    "get_funnel_counts": get_funnel_counts,
    "get_option_snapshot": get_option_snapshot,
    "get_underlying_bars": get_underlying_bars,
    "get_iv_rank": get_iv_rank,
    "get_position_snapshot": get_position_snapshot,
    "get_entry_thesis": get_entry_thesis,
}

__all__ = ["REGISTRY", "Handler"]
