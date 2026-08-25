"""Human-driven entry points — the ones you run to watch the system work.

    council      One council pass over a symbol, printed. Also reachable as
                 ``python -m trading_agents``.
    reflection   One Reflection Agent pass over a decision log.

Both degrade to MOCK mode without ANTHROPIC_API_KEY, so either is a valid
offline smoke test:

    uv run --package agents python -m trading_agents --symbol NVDA
    uv run --package agents python -m trading_agents.cli.reflection --since 24h

Reflection is deliberately a SEPARATE process from the council: it must
never share a runtime with a live council pass.
"""
