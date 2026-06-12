"""Thin Anthropic SDK wrapper + deterministic mock fallback.

When ``ANTHROPIC_API_KEY`` is unset, the client returns canned JSON for every
call. The mock is keyed on the SYSTEM PROMPT keywords so each node still gets
a structurally appropriate response. This keeps the council runnable in CI,
on a fresh laptop, and during the 5-month paper-trading phase without
burning cents per smoke test.

When the key is set, calls go to the real Anthropic Messages API with prompt
caching enabled (5-min TTL — matches PLAN.md §9 model-cost strategy).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("agents.llm")


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name)
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


class Model:
    OPUS = "claude-opus-4-7"
    SONNET = "claude-sonnet-4-6"
    HAIKU = "claude-haiku-4-5-20251001"


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


class LLM:
    """Single entry point for every LLM call in the council.

    Real mode requires ``anthropic>=0.40`` installed AND ``ANTHROPIC_API_KEY``
    set. Anything else triggers mock mode.
    """

    def __init__(self, api_key: str | None = None) -> None:
        env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self._api_key = api_key or (env_key or None)
        self._client: Any = None
        # Empty string or missing → mock. Treat whitespace-only the same way so
        # an accidentally-blanked-out export doesn't crash on the first call.
        self._mock = not self._api_key
        if not self._mock:
            try:
                import anthropic  # noqa: F401
            except ImportError:
                logger.warning("anthropic SDK not installed — falling back to MOCK mode")
                self._mock = True
        if self._mock:
            # Production guard: a misconfigured box must FAIL, not silently
            # emit canned MOCK theses into a real user's approval inbox.
            if _env_truthy("AGENTS_REQUIRE_REAL_LLM"):
                raise RuntimeError(
                    "AGENTS_REQUIRE_REAL_LLM=1 but the LLM resolved to MOCK mode "
                    "(ANTHROPIC_API_KEY missing/blank or SDK not installed). "
                    "Refusing to run the council on canned responses."
                )
            logger.warning("LLM in MOCK mode (no ANTHROPIC_API_KEY)")

    @property
    def mock(self) -> bool:
        return self._mock

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic
            # Explicit timeout: a hung API call must never hang the council.
            # The SDK retries transient failures itself (max_retries).
            self._client = AsyncAnthropic(
                api_key=self._api_key,
                timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
                max_retries=2,
            )
        return self._client

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str = Model.SONNET,
        max_tokens: int = 800,
        cache_system: bool = True,
    ) -> LLMResponse:
        if self._mock:
            resp = _mock_response(system=system, user=user, model=model)
            await _record_to_ledger(system, resp, is_mock=True)
            return resp

        client = self._get_client()
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        msg = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
            # temperature=0 by default: council variance should come from the
            # market, not the sampler. Override via LLM_TEMPERATURE if a node
            # ever needs creative range (document why before raising it).
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.0")),
        )

        text = msg.content[0].text if msg.content else ""
        usage = getattr(msg, "usage", None)
        resp = LLMResponse(
            text=text,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) if usage else 0,
        )
        await _record_to_ledger(system, resp, is_mock=False)
        return resp

    @staticmethod
    def parse_json(text: str) -> dict[str, Any]:
        """Lenient JSON parse — strips Markdown fences if the model wrapped its output."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(json)?\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        return json.loads(cleaned.strip())



# ─────────────────────────────────────────────────────────────────────
# JSON-call helper — works with ANY object exposing ``complete()``
# (the real LLM, the mock, and the narrow test doubles in the suite).
# ─────────────────────────────────────────────────────────────────────


async def complete_json(
    llm: Any,
    *,
    system: str,
    user: str,
    model: str = Model.SONNET,
    max_tokens: int = 800,
    cache_system: bool = True,
) -> tuple[dict[str, Any] | None, bool]:
    """``llm.complete()`` + parse, with ONE re-ask on malformed output.

    Returns ``(data, degraded)``:
      - ``(dict, False)``  first response parsed.
      - ``(dict, True)``   first response was malformed; the retry parsed.
      - ``(None, True)``   both attempts malformed — the caller applies its
        neutral fallback AND must surface the degraded flag so the decision
        row records that this run ran on fallbacks (a degraded run changing
        the decision silently was audit finding §4.1).
    """
    resp = await llm.complete(
        system=system, user=user, model=model,
        max_tokens=max_tokens, cache_system=cache_system,
    )
    try:
        return LLM.parse_json(resp.text), False
    except Exception as exc:  # noqa: BLE001 — malformed output, not a bug
        logger.warning("complete_json: parse failed (%s) — re-asking once", exc)

    retry_user = (
        f"{user}\n\nREMINDER: your previous reply was not valid JSON. "
        "Respond with the JSON object ONLY — no prose, no markdown fences."
    )
    resp = await llm.complete(
        system=system, user=retry_user, model=model,
        max_tokens=max_tokens, cache_system=cache_system,
    )
    try:
        return LLM.parse_json(resp.text), True
    except Exception as exc:  # noqa: BLE001
        logger.error("complete_json: retry also malformed (%s) — degraded", exc)
        return None, True


# ─────────────────────────────────────────────────────────────────────
# Mock response generator — keyed on system-prompt keywords
# ─────────────────────────────────────────────────────────────────────


def _extract_symbol(user: str) -> str:
    """Pull a ticker out of the user prompt so the mock response feels grounded."""
    match = re.search(r"Ticker:\s*([A-Z][A-Z0-9.\-]{0,9})", user)
    if match:
        return match.group(1)
    match = re.search(r"\b([A-Z]{2,5})\b", user)
    return match.group(1) if match else "AAPL"


def _mock_response(*, system: str, user: str, model: str) -> LLMResponse:
    """Branch on the role declared in the prompt's opening line.

    Every prompt starts with ``You are the <Role>`` — we anchor on that
    rather than scanning the whole system text. This avoids false matches
    when a different role's prompt happens to mention the word "Router"
    or "Classify" in a sentence (which is what caused the first version of
    this function to misroute Macro to the Router branch).
    """
    sym = _extract_symbol(user)
    # Look only at the role declaration. Anchored, case-insensitive.
    role_line = system[:120].lower()

    if "you are the router" in role_line:
        body = {
            "regime": "bull",
            "analyst_subset": ["technical", "fundamental", "macro"],
            "rationale": (
                "MOCK: trend filter intact, breadth healthy, vol regime constructive. "
                "Running technical + fundamental + macro for full coverage in this demo."
            ),
        }
    elif "you are the technical analyst" in role_line:
        body = {
            "score": 64.0,
            "confidence": 0.62,
            "thesis": (
                f"MOCK: {sym} is above the 50-DMA with RSI in the upper-50s. "
                "Volume profile constructive. Mean-reversion risk muted."
            ),
            "citations": ["50dma_position", "rsi_14", "volume_ratio_20d"],
        }
    elif "you are the fundamental analyst" in role_line:
        body = {
            "score": 58.0,
            "confidence": 0.55,
            "thesis": (
                f"MOCK: {sym} quality scores firm; earnings revisions positive over last 30d. "
                "Valuation in line with peers."
            ),
            "citations": ["quality_score", "earnings_revisions"],
        }
    elif "you are the macro analyst" in role_line:
        body = {
            "score": 60.0,
            "confidence": 0.50,
            "thesis": (
                f"MOCK: macro regime supportive for {sym}. VIX moderate, 10y stable, "
                "DXY in a normal band. Sector relative strength positive — no macro headwind."
            ),
            "citations": ["vix_level", "ten_year_yield_pct", "sector_relative_strength"],
        }
    elif "you are the strategy selector" in role_line:
        body = {
            "strategy": "momentum",
            "confidence": 0.58,
            "rationale": (
                f"MOCK: Trend regime + positive analyst scores on {sym} point at the 12-1 momentum "
                "strategy. Counter-trend setups are weak; breakout signal absent."
            ),
        }
    elif "you are the proposal drafter" in role_line:
        body = {
            "verdict": "BUY",
            "confidence": 0.58,
            "rationale": (
                f"MOCK: Council leans positive on {sym}. Strategy fits the regime; entering at "
                "market with ATR-driven stop."
            ),
            "bull_case": (
                f"Technical setup constructive on {sym}; momentum cluster aligns with sector strength. "
                "Pullback to 50-DMA already absorbed; volume confirms accumulation."
            ),
            "bear_case": (
                f"If broader risk-off resumes, {sym} compresses fast. Insider activity flat, no insider tailwind. "
                "Earnings in two weeks add binary risk."
            ),
            "risk_level": 2,
            "conviction_level": 3,
        }
    elif "you are the reflection agent" in role_line:
        # Mock review — small positive nudge with a generic lesson. The
        # store will clamp regardless; we just need a deterministic shape.
        body = {
            "strategy_id": "momentum",
            "wins": 2,
            "losses": 1,
            "avg_winner_pct": 3.4,
            "avg_loser_pct": -1.8,
            "lessons": [
                "MOCK: trend regime + tech score >60 paired with a 2:1 win rate.",
                "MOCK: losers concentrated when macro score dropped below 50 mid-hold.",
            ],
            "confidence_delta": 0.04,
            "notes": "MOCK: small positive nudge; sample size small.",
        }
    else:
        # Generic fallback so we never raise on an unrecognized prompt.
        body = {"score": 50.0, "confidence": 0.2, "thesis": "MOCK: generic neutral response."}

    return LLMResponse(text=json.dumps(body), model=f"{model}+mock")


# ─────────────────────────────────────────────────────────────────────
# Cost-ledger hook
#
# Wired here so every call site is automatically tracked. The ledger
# import is local + try-wrapped so a misconfigured ledger never breaks
# the council — telemetry is best-effort.
# ─────────────────────────────────────────────────────────────────────


async def _record_to_ledger(system: str, resp: "LLMResponse", *, is_mock: bool) -> None:
    try:
        from trading_agents.cost_ledger import (
            LedgerEntry,
            compute_cost_usd,
            get_cost_ledger,
            infer_role_from_system_prompt,
        )

        cost = compute_cost_usd(
            model=resp.model,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cache_read_tokens=resp.cache_read_tokens,
            cache_creation_tokens=resp.cache_creation_tokens,
        )
        await get_cost_ledger().record(
            LedgerEntry(
                model=resp.model.split("+", 1)[0],
                role=infer_role_from_system_prompt(system),
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cache_read_tokens=resp.cache_read_tokens,
                cache_creation_tokens=resp.cache_creation_tokens,
                cost_usd=cost,
                is_mock=is_mock,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cost ledger write failed (best-effort): %s", exc)
