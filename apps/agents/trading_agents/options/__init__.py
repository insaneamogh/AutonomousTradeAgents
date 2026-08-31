"""Two arguing options agents (Bull/Bear) with guarded, real trade tools.

See ``docs/IMPL_OPTIONS_AGENTS.md`` and ``docs/PLAN_OPTIONS_AGENTS.md`` for the
full design. The agents never call ``packages/broker`` directly and never
choose a strike, expiry or quantity — every mutating tool call routes through
``tools.guard.ToolGuard``, which derives the contract deterministically
(``packages/engine/engine/options/selection.py::select_contract``) and re-runs
the full options risk engine before anything reaches the broker.

``agents.py``/``resolution.py``/``prompts.py`` have landed, so the top-level
re-exports below cover the whole flow: run the argument phase, resolve it,
and (only on agreement) run the guarded trade hop. Importing from
``trading_agents.options.tools`` directly still works unchanged for anyone
who only needs the guard/registry/schemas.
"""

from __future__ import annotations

from trading_agents.options.agents import (
    OptionsAgentsResult,
    run_bear,
    run_bull,
    run_bull_and_bear,
    run_options_agents,
)
from trading_agents.options.escalation import (
    EscalationBudget,
    EscalationOutcome,
    EscalationTrigger,
    PositionBrief,
    evaluate_escalation_trigger,
    maybe_escalate,
    run_escalation,
)
from trading_agents.options.prompts import OPTIONS_BEAR, OPTIONS_BULL, OPTIONS_ESCALATION
from trading_agents.options.resolution import AgentView, Resolution, resolve

__all__ = [
    "OPTIONS_BEAR",
    "OPTIONS_BULL",
    "OPTIONS_ESCALATION",
    "AgentView",
    "EscalationBudget",
    "EscalationOutcome",
    "EscalationTrigger",
    "OptionsAgentsResult",
    "PositionBrief",
    "Resolution",
    "evaluate_escalation_trigger",
    "maybe_escalate",
    "resolve",
    "run_bear",
    "run_bull",
    "run_bull_and_bear",
    "run_escalation",
    "run_options_agents",
]
