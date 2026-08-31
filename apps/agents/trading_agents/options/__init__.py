"""Two arguing options agents (Bull/Bear) with guarded, real trade tools.

See ``docs/IMPL_OPTIONS_AGENTS.md`` and ``docs/PLAN_OPTIONS_AGENTS.md`` for the
full design. The agents never call ``packages/broker`` directly and never
choose a strike, expiry or quantity — every mutating tool call routes through
``tools.guard.ToolGuard``, which derives the contract deterministically
(``packages/engine/engine/options/selection.py::select_contract``) and re-runs
the full options risk engine before anything reaches the broker. This module
is intentionally empty of re-exports until ``agents.py``/``resolution.py``
land — importing from ``trading_agents.options.tools`` directly is fine in
the meantime.
"""

from __future__ import annotations
