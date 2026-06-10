"""CLI smoke test:

    uv run python -m trading_agents --symbol NVDA
    uv run python -m trading_agents --symbol AAPL --horizon mid --no-langgraph

Without ANTHROPIC_API_KEY the LLM falls back to mock mode — the smoke test
still produces a structurally valid result, useful for CI and offline dev.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from trading_agents.llm import LLM
from trading_agents.runtime import run_council

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s — %(message)s")
log = logging.getLogger("agents.cli")


async def _main(symbol: str, horizon: str, force_fallback: bool) -> int:
    llm = LLM()
    log.info("LLM mode: %s", "MOCK" if llm.mock else "REAL")
    log.info("Running council for %s (horizon=%s)…", symbol, horizon)

    # `force_fallback` is honored only via the graph helper; the runtime
    # picks LangGraph automatically. Simplest way to override during the
    # smoke is to monkey-patch the flag here.
    if force_fallback:
        import trading_agents.graph as graph_mod
        graph_mod._HAS_LANGGRAPH = False  # noqa: SLF001

    result = await run_council(symbol=symbol, horizon=horizon, llm=llm)  # type: ignore[arg-type]

    print("\n=== COUNCIL RESULT ===")
    print(f"Symbol:         {symbol}")
    print(f"Regime:         {result.get('regime')}")
    print(f"Final action:   {result['final_action']}")
    print(f"Risk approved:  {result['risk_approved']}  ({result['risk_reason']})")
    if result.get("risk_veto_rule"):
        print(f"Veto rule:      {result['risk_veto_rule']}")
    if result.get("technical"):
        t = result["technical"]
        print(f"Technical:      score={t['score']:.1f} conf={t['confidence']:.2f}")
    if result.get("fundamental"):
        f = result["fundamental"]
        print(f"Fundamental:    score={f['score']:.1f} conf={f['confidence']:.2f}")
    if result.get("macro"):
        m = result["macro"]
        print(f"Macro:          score={m['score']:.1f} conf={m['confidence']:.2f}")
    sel = result.get("selected_strategy")
    if sel:
        print(
            f"Selector:       strategy={sel} conf={result.get('selector_confidence', 0.0):.2f}"
        )
        if result.get("selector_rationale"):
            print(f"                {result['selector_rationale']}")
    else:
        print("Selector:       HOLD (no strategy fit)")
        if result.get("selector_rationale"):
            print(f"                {result['selector_rationale']}")
    print("\n--- PROPOSAL (DTO) ---")
    print(json.dumps(result["proposal"], indent=2) if result["proposal"] else "(none — HOLD or VETOED)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Council smoke test.")
    parser.add_argument("--symbol", default="NVDA")
    parser.add_argument("--horizon", default="short", choices=["intraday", "short", "mid", "long"])
    parser.add_argument(
        "--no-langgraph",
        action="store_true",
        help="Force the plain-asyncio fallback even if langgraph is installed",
    )
    args = parser.parse_args()
    return asyncio.run(_main(args.symbol, args.horizon, args.no_langgraph))


if __name__ == "__main__":
    sys.exit(main())
