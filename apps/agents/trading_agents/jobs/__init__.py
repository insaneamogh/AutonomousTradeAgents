"""Scheduled jobs — the unattended entry points.

    daily_cron   Runs the council once per user per symbol per trading day,
                 then hands off to ghost_eval. Idempotent by (user, symbol,
                 UTC date), so a retried cron is harmless.
    ghost_eval   Prices what the non-executed picks WOULD have done, so the
                 council can be scored on proposals it never got to place.

Run either standalone:

    uv run --package agents python -m trading_agents.jobs.daily_cron --force
    uv run --package agents python -m trading_agents.jobs.ghost_eval

These live inside the package (rather than a loose ``scripts/`` directory)
so they import like everything else and the tests need no sys.path surgery.
"""
