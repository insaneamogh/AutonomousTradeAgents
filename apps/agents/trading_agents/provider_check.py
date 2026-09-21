"""Verify an LLM provider is reachable and correctly priced. No trades.

Run this FIRST after setting keys on Railway, before letting a scheduled
council pass discover the problem at 09:30 with real money on the line:

    railway run -s AutonomousTradeAgents \\
        python -m trading_agents.provider_check --live

Without `--live` it is pure configuration inspection — no network, no
spend, safe anywhere. With `--live` it makes exactly ONE call and reports
what came back, what it cost, and how long it took.

It never prints an API key, only whether one is present and how long it is.
This output is meant to be pasteable into a bug report.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

from trading_agents import jev
from trading_agents.cost_ledger import _PRICES, compute_cost_usd
from trading_agents.llm import LLM, Model, _key_for_provider, active_provider, resolve_model

_SYSTEM = (
    "You are a disciplined equity analyst. Judge only from the supplied "
    "evidence and state a direction."
)
_USER = (
    "SPY. 20-day SMA 512.40, 50-day SMA 505.10, last 518.90, RSI(14) 58.2, "
    "ATR(14) 4.80, 20-day realised vol 12.1%. Volume 1.1x its 20-day average."
)
"""A fixed, synthetic snapshot. Deliberately NOT a live quote: the point is
to prove the transport works, and a probe whose input changes every run
cannot tell a provider outage from the market simply having moved."""


def _mask(key: str) -> str:
    return f"present ({len(key)} chars)" if key else "MISSING"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--live",
        action="store_true",
        help="make one real call (costs money; without this, nothing leaves the box)",
    )
    ap.add_argument("--provider", help="override LLM_PROVIDER for this check only")
    args = ap.parse_args()

    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider

    provider = active_provider()
    key = _key_for_provider(provider)

    print(f"provider          {provider}")
    print(f"  LLM_PROVIDER    {os.environ.get('LLM_PROVIDER', '<unset -> anthropic>')!r}")
    print(f"  key             {_mask(key)}")
    if provider == "glm":
        base = os.environ.get("GLM_BASE_URL", "").strip()
        print(f"  base_url        {base or 'https://api.z.ai/api/anthropic (default)'}")
        print("  key env         GLM_API_KEY (or ZAI_API_KEY)")
    elif provider == "jev":
        ep = os.environ.get("JEV_ENDPOINT", "").strip()
        print(f"  endpoint        {ep or jev.ENDPOINT + ' (default)'}")
        print("  key env         TYPESAFE_API_KEY")
    else:
        print("  key env         ANTHROPIC_API_KEY")

    print("\nresolved models")
    for tier, name in (("opus", Model.OPUS), ("sonnet", Model.SONNET), ("haiku", Model.HAIKU)):
        wire = resolve_model(name, provider=provider)
        priced = "priced" if wire in _PRICES else "UNPRICED -> billed at Sonnet rates"
        print(f"  {tier:<8} {name:<28} -> {wire:<18} [{priced}]")

    llm = LLM()
    print(f"\nmode              {'MOCK (no usable key)' if llm.mock else 'LIVE'}")
    if llm.mock and not args.live:
        print("\nConfiguration only. Set the key above, then re-run with --live.")
        return 0
    if llm.mock:
        print("\nFAIL: --live requested but the client resolved to MOCK.")
        print("      The key for this provider is missing, blank, or a placeholder.")
        return 1
    if not args.live:
        print("\nKey looks usable. Re-run with --live to make one real call.")
        return 0

    print(f"\ncalling {provider}...")
    t0 = time.monotonic()
    d = await llm.decide(system=_SYSTEM, user=_USER)
    elapsed = time.monotonic() - t0

    print(f"  elapsed         {elapsed:.2f}s")
    print(f"  model           {d.model}")
    print(f"  direction       {d.direction}")
    print(f"  conviction      {d.conviction:.2f}")
    print(f"  confidence      {d.confidence:.2f}")
    print(f"  abstained       {d.abstained}")
    if d.thesis:
        print(f"  thesis          {d.thesis[:160]}")

    if d.abstained:
        # An abstain here is a FAILED probe, not a neutral market view: the
        # snapshot above is fixed and unambiguous, so "no answer" means the
        # call or the parse broke. Check the log line above this output.
        print("\nFAIL: the provider abstained — transport or contract failure.")
        return 1

    per_pass = compute_cost_usd(model=d.model, input_tokens=4000, output_tokens=600)
    print(f"\nOK. A ~4k-in/600-out council call on {d.model} costs ${per_pass:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
