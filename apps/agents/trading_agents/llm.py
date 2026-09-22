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
import math
import os
import re
from dataclasses import dataclass
from typing import Any

from engine.env import env_flag
from trading_agents import jev

logger = logging.getLogger("agents.llm")


class Model:
    OPUS = "claude-opus-4-7"
    SONNET = "claude-sonnet-4-6"
    HAIKU = "claude-haiku-4-5-20251001"


# ── Provider routing ──────────────────────────────────────────────────
#
# The council's Sonnet calls were 88% of this account's LLM spend ($9.49
# of $10.74), which is what made the operator pull the API key and stop
# the desk. Z.ai serves an ANTHROPIC-COMPATIBLE endpoint, so GLM is a
# base-URL swap rather than a second client: the SDK, the tool-calling
# shape and every caller stay exactly as they are.
#
# Deliberately opt-in and deliberately per-tier. The council's job is
# judgement, and a cheaper model that abstains more or reasons worse
# costs far more in bad trades than it saves in tokens — so this exists
# to be A/B'd against the metrics we now have (abstain rate, directional
# accuracy, MFE), not to be flipped on and forgotten.

_GLM_BASE_URL = "https://api.z.ai/api/anthropic"

_JEV_MODEL = jev.MODEL
"""TypeSafe Jev, imported rather than restated — the first version of
this file hardcoded "jev-1.13" while the real id is "jev-1.13.0", so the
cost ledger had no price row for it and would have billed Jev at
Sonnet's rate (CLAUDE.md §4.4 — the same number in two places).

**Not a chat model** — it answers typed questions and
returns typed answers, and generates no prose at all. It therefore cannot
serve `complete()` or `complete_with_tools()`, only the structured
`decide()` path (see `trading_agents.jev`). Routing to it for a prose or
tool call falls back to mock rather than silently returning nothing."""

_GLM_MODEL_MAP: dict[str, str] = {
    Model.OPUS: "glm-5.3",
    Model.SONNET: "glm-5.3",
    Model.HAIKU: "glm-5.3-flash",
}
"""Claude tier -> GLM model. Mapped by TIER, not by name, so a caller
asking for `Model.SONNET` keeps asking for "the reasoning tier" and this
table decides what serves it.

Current Z.ai generation as of 2026-09-23 (docs.z.ai pricing): glm-5.3 at
$1.40/$4.40 per M for reasoning, glm-5.3-flash at $0.15/$0.50 for the fast
tier. Every default here MUST have a `cost_ledger._PRICES` row —
`test_every_default_glm_tier_is_priced` fails otherwise, because an
unpriced id is billed at Sonnet rates and the $3/day cap trips on spend
that never happened.

These are DEFAULTS, overridable per tier from the environment (below).
Z.ai revs GLM faster than we deploy, and a model id going stale must be a
Railway variable change, not a code change and a redeploy. Point a tier at
a model with no price row and the ledger warns that it is billing it at
Sonnet rates until a row is added."""

_GLM_TIER_ENV = {
    Model.OPUS: "GLM_MODEL_OPUS",
    Model.SONNET: "GLM_MODEL_SONNET",
    Model.HAIKU: "GLM_MODEL_HAIKU",
}


_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "glm": "GLM_API_KEY",
    "jev": "TYPESAFE_API_KEY",
}
"""Each provider reads its OWN key. Two reasons, both load-bearing:
selecting a provider must never spend a different provider's key, and the
cheaper providers must be reachable WITHOUT restoring the Anthropic key —
which is the situation we are actually in, since it was removed on
2026-09-11 to stop the spend."""


def _key_for_provider(provider: str) -> str:
    if provider == "glm":
        return (
            os.environ.get("GLM_API_KEY", "").strip()
            or os.environ.get("ZAI_API_KEY", "").strip()
        )
    return os.environ.get(_KEY_ENV.get(provider, "ANTHROPIC_API_KEY"), "").strip()


def active_provider() -> str:
    """`"glm"`, `"jev"`, or `"anthropic"` (the default). Anything
    unrecognised falls back to anthropic rather than erroring — a typo in
    an env var must not take the desk down."""
    raw = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if raw in ("glm", "jev"):
        return raw
    if raw and raw != "anthropic":
        logger.warning(
            "ignoring unknown LLM_PROVIDER=%r — using anthropic", raw
        )
    return "anthropic"


def resolve_model(model: str, *, provider: str | None = None) -> str:
    """The model id to actually send, for the active provider.

    An unmapped model passes through unchanged: better to send a name the
    provider may accept than to silently substitute a different one."""
    p = provider or active_provider()
    if p == "jev":
        # Jev has one model and one shape; tiers are meaningless to it.
        return _JEV_MODEL
    if p != "glm":
        return model
    override = os.environ.get(_GLM_TIER_ENV.get(model, ""), "").strip()
    return override or _GLM_MODEL_MAP.get(model, model)


_JEV_NO_THESIS = "Typed assessment (Jev). No written thesis generated."

_DECISION_CONTRACT = """

Reply with ONLY a JSON object, no prose and no code fence:
{"direction": "bullish"|"bearish"|"neutral",
 "conviction": <0.0-1.0, how strongly the evidence supports the case>,
 "confidence": <0.0-1.0, how certain you are of the direction>,
 "thesis": "<one or two sentences>"}
Judge only from the supplied evidence. `conviction` is the strength of the
case, NOT your certainty and NOT the probability of profit."""
"""Appended to the system prompt on the prose providers so their answer has
the same shape as Jev's typed one. Kept as a suffix rather than a rewrite:
the caller's own prompt still governs WHAT is judged, this governs only how
the judgement comes back."""


def _unit(value: object) -> float:
    """Clamp to [0, 1]. A model that returns 85 when asked for 0-1 means 0.85
    often enough to be worth handling, and a model that returns nonsense must
    become 0.0 rather than an exception inside a scoring path."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    if 1.0 < f <= 100.0:
        f /= 100.0
    return min(1.0, max(0.0, f))


@dataclass(frozen=True)
class Decision:
    """One directional judgement, identical in shape across every provider.

    The A/B unit. Jev, GLM and Sonnet all produce this, so `abstained` rate,
    directional accuracy and realised MFE are comparable between them
    without a per-provider adapter in the measurement code.
    """

    direction: str
    """bullish | bearish | neutral."""

    conviction: float
    """0-1, strength of the case. Forced to 0.0 when neutral by
    `__post_init__` — a neutral read with high conviction is not a thing,
    and letting one through is how a "no view" gets sized like a view."""

    confidence: float
    """0-1, the model's certainty in `direction`. Kept separate from
    `conviction` on purpose; collapsing them is how a confident read of a
    weak setup becomes a large position."""

    thesis: str
    provider: str
    model: str

    abstained: bool = False
    """True when the model could not be reached or its answer did not parse.
    A caller MUST treat this differently from a genuine neutral: one is "the
    evidence says nothing", the other is "we never got an answer"."""

    def __post_init__(self) -> None:
        """The neutral-means-zero rule, enforced in exactly ONE place.

        It used to live in three — `jev.parse_response`, `conviction_0_1`
        and the prose branch of `decide()` — which is the CLAUDE.md §4.4
        trap: with three copies, breaking any one of them leaves the others
        covering, so no test could prove any of them worked. Enforcing it on
        the type makes an unsized neutral structurally unconstructible, and
        makes a single test meaningful.
        """
        if self.direction == "neutral" and self.conviction != 0.0:
            object.__setattr__(self, "conviction", 0.0)


@dataclass(frozen=True)
class ToolCall:
    """One `tool_use` block the model emitted."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str | None = None


def _extract_blocks(msg: Any) -> tuple[str, tuple[ToolCall, ...]]:
    """Walk every content block instead of assuming content[0] is text.

    The old `msg.content[0].text` broke on any response whose FIRST block
    is not text — which is every tool-using response, and also a plain
    text response that happens to lead with a thinking block. Behaviour is
    identical for today's five council nodes (one text block in, the same
    string out); it is strictly more robust for everything else.
    """
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in msg.content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            texts.append(block.text)
        elif btype == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input or {})))
    return "\n".join(texts), tuple(calls)


class LLM:
    """Single entry point for every LLM call in the council.

    Real mode requires ``anthropic>=0.40`` installed AND ``ANTHROPIC_API_KEY``
    set. Anything else triggers mock mode.
    """

    # Values ops teams leave as stand-ins. A non-empty placeholder must mean
    # MOCK, not "attempt real calls and 401 on every council node".
    _PLACEHOLDER_KEYS = frozenset({"replace_me", "changeme", "change_me", "placeholder", "todo", "xxx"})

    def __init__(self, api_key: str | None = None) -> None:
        self._provider = active_provider()
        # GLM reads its own key, so pointing LLM_PROVIDER at it cannot
        # accidentally spend an Anthropic key, and removing the Anthropic
        # key does not disable GLM. The two are independent on purpose:
        # the operator pulled ANTHROPIC_API_KEY to stop the spend, and the
        # cheaper provider has to be reachable without putting it back.
        env_key = _key_for_provider(self._provider)
        self._api_key = api_key or (env_key or None)
        self._client: Any = None
        # Empty string or missing → mock. Treat whitespace-only the same way so
        # an accidentally-blanked-out export doesn't crash on the first call.
        if self._api_key and self._api_key.lower() in self._PLACEHOLDER_KEYS:
            logger.warning(
                "%s is the placeholder %r — treating as unset (MOCK mode). "
                "Set a real key to enable live council reasoning.",
                self._key_env_name, self._api_key,
            )
            self._api_key = None
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
            if env_flag("AGENTS_REQUIRE_REAL_LLM"):
                raise RuntimeError(
                    "AGENTS_REQUIRE_REAL_LLM=1 but the LLM resolved to MOCK mode "
                    f"({self._key_env_name} missing/blank or SDK not installed). "
                    "Refusing to run the council on canned responses."
                )
            logger.warning(
                "LLM in MOCK mode (no %s)", self._key_env_name
            )

    @property
    def _key_env_name(self) -> str:
        return _KEY_ENV[self._provider]

    @property
    def mock(self) -> bool:
        return self._mock

    @property
    def provider(self) -> str:
        return self._provider

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic
            # Explicit timeout: a hung API call must never hang the council.
            # The SDK retries transient failures itself (max_retries).
            # `base_url` is the whole GLM integration: Z.ai speaks the
            # Anthropic wire protocol, so the SDK, the tool-calling shape
            # and every caller are untouched.
            kwargs: dict[str, Any] = {
                "api_key": self._api_key,
                # Explicit timeout: a hung API call must never hang the
                # council. The SDK retries transient failures itself.
                "timeout": float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
                "max_retries": 2,
            }
            if self._provider == "glm":
                kwargs["base_url"] = os.environ.get(
                    "GLM_BASE_URL", _GLM_BASE_URL
                ).strip() or _GLM_BASE_URL
                # Z.ai documents its Anthropic-compatible endpoint with
                # ANTHROPIC_AUTH_TOKEN, i.e. `Authorization: Bearer`, while
                # `api_key` alone makes the SDK send only `X-Api-Key`. With
                # both set the SDK sends both headers, and both carry the GLM
                # key, so the endpoint authenticates whichever it reads.
                # `api_key` stays explicitly set for a second reason: were it
                # None, the SDK would fall back to ANTHROPIC_API_KEY from the
                # environment and send an Anthropic key to Z.ai.
                kwargs["auth_token"] = self._api_key
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str = Model.SONNET,
        max_tokens: int = 800,
        cache_system: bool = True,
        council_run_id: str | None = None,
        agent_decision_id: str | None = None,
        user_id: str | None = None,
    ) -> LLMResponse:
        # ``council_run_id`` / ``agent_decision_id`` / ``user_id`` are optional
        # passthrough correlators for the cost ledger (cost_ledger.LedgerEntry)
        # — purely additive, every existing call site is unaffected. In
        # practice a caller inside a live council pass only ever has
        # ``council_run_id`` + ``user_id`` on hand: the real decision id does
        # not exist until strictly after every LLM call in the pass completes
        # (see runtime.run_council). ``agent_decision_id`` is wired through
        # end to end for symmetry / any future direct-attribution caller, but
        # must never carry a live, not-yet-persisted decision id —
        # llm_calls.agent_decision_id has a real (non-deferrable) FK, and
        # writing one before the row exists reintroduces the exact
        # ForeignKeyViolation this design avoids.
        if self._mock:
            resp = _mock_response(system=system, user=user, model=model)
            await _record_to_ledger(
                system,
                resp,
                is_mock=True,
                council_run_id=council_run_id,
                agent_decision_id=agent_decision_id,
                user_id=user_id,
            )
            return resp

        client = self._get_client()
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        # The wire model is provider-specific; `resp.model` below keeps the
        # RESOLVED id so the cost ledger prices what was actually billed.
        wire_model = resolve_model(model, provider=self._provider)
        msg = await client.messages.create(
            model=wire_model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
            # temperature=0 by default: council variance should come from the
            # market, not the sampler. Override via LLM_TEMPERATURE if a node
            # ever needs creative range (document why before raising it).
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.0")),
        )

        text, tool_calls = _extract_blocks(msg)
        usage = getattr(msg, "usage", None)
        resp = LLMResponse(
            text=text,
            model=wire_model,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) if usage else 0,
            tool_calls=tool_calls,
            stop_reason=getattr(msg, "stop_reason", None),
        )
        await _record_to_ledger(
            system,
            resp,
            is_mock=False,
            council_run_id=council_run_id,
            agent_decision_id=agent_decision_id,
            user_id=user_id,
        )
        return resp

    async def complete_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str = Model.SONNET,
        max_tokens: int = 2048,
        tool_choice: dict[str, Any] | None = None,
        cache_system: bool = True,
        council_run_id: str | None = None,
        agent_decision_id: str | None = None,
        user_id: str | None = None,
    ) -> LLMResponse:
        """One tool-enabled turn. The LOOP lives in the caller, not here —
        this method is a single request/response so the caller owns how
        many rounds it is willing to pay for (see ``llm_loop.run_tool_loop``).

        Unlike ``complete()``, ``messages`` is caller-supplied rather than
        hardcoded to a single user turn — a tool loop needs to append the
        assistant's ``tool_use`` turn and the following ``tool_result`` turn
        onto the same list across rounds.
        """
        if self._mock:
            # The mock is TEXT ONLY (see `_mock_response`) — it never emits
            # a `tool_use` block. `run_tool_loop` terminates the round trip
            # on "no tool calls"; a mock that emitted one would loop until
            # `max_rounds` on every test that touches this path.
            resp = _mock_response(system=system, user=_flatten(messages), model=model)
            await _record_to_ledger(
                system,
                resp,
                is_mock=True,
                council_run_id=council_run_id,
                agent_decision_id=agent_decision_id,
                user_id=user_id,
            )
            return resp

        client = self._get_client()
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        wire_model = resolve_model(model, provider=self._provider)
        kwargs: dict[str, Any] = {
            "model": wire_model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": messages,
            "temperature": float(os.environ.get("LLM_TEMPERATURE", "0.0")),
            "tools": tools,
        }
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        msg = await client.messages.create(**kwargs)

        text, tool_calls = _extract_blocks(msg)
        usage = getattr(msg, "usage", None)
        resp = LLMResponse(
            text=text,
            model=wire_model,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) if usage else 0,
            tool_calls=tool_calls,
            stop_reason=getattr(msg, "stop_reason", None),
        )
        await _record_to_ledger(
            system,
            resp,
            is_mock=False,
            council_run_id=council_run_id,
            agent_decision_id=agent_decision_id,
            user_id=user_id,
        )
        return resp

    async def decide(
        self,
        *,
        system: str,
        user: str,
        model: str = Model.SONNET,
        max_tokens: int = 600,
        council_run_id: str | None = None,
        agent_decision_id: str | None = None,
        user_id: str | None = None,
    ) -> Decision:
        """One structured directional judgement, on whichever provider is active.

        This is the seam that makes Jev usable at all. Jev cannot serve
        `complete()` — it generates no prose — so a provider flag alone
        could never route the council to it. What every provider CAN do is
        answer "which way, and how strongly", and that is the only part of
        an agent's output that survives into a position anyway: the thesis
        is audit trail, the direction and conviction are the trade.

        On Jev this is one native typed call. On Anthropic and GLM it is a
        JSON completion shaped to the same contract, so an A/B compares
        like with like rather than comparing two different questions.

        **Failure contract, adopted deliberately (see PLAN §2.3):** a model
        call or parse failure ABSTAINS — `Decision.abstained` is True and
        the direction is neutral. It never raises. A *data* failure, by
        contrast, must propagate from the caller's feature layer and never
        reach here, because a broken snapshot silently becoming "no view"
        is how a bug turns into a quiet, permanent HOLD.
        """
        if self._mock:
            resp = _mock_response(system=system, user=user, model=model)
            await _record_to_ledger(
                system, resp, is_mock=True,
                council_run_id=council_run_id,
                agent_decision_id=agent_decision_id,
                user_id=user_id,
            )
            return Decision(
                direction="neutral", conviction=0.0, confidence=0.0,
                thesis="MOCK: no provider key configured.",
                provider=self._provider, model=resp.model, abstained=True,
            )

        if self._provider == "jev":
            return await self._decide_jev(
                system=system, user=user,
                council_run_id=council_run_id,
                agent_decision_id=agent_decision_id,
                user_id=user_id,
            )

        try:
            resp = await self.complete(
                system=system + _DECISION_CONTRACT,
                user=user,
                model=model,
                max_tokens=max_tokens,
                council_run_id=council_run_id,
                agent_decision_id=agent_decision_id,
                user_id=user_id,
            )
            raw = self.parse_json(resp.text)
            direction = str(raw["direction"]).strip().lower()
            if direction not in jev.DIRECTIONS:
                raise ValueError(f"direction {direction!r} not in {jev.DIRECTIONS}")
            return Decision(
                direction=direction,
                conviction=_unit(raw.get("conviction")),  # zeroed if neutral by __post_init__
                confidence=_unit(raw.get("confidence")),
                thesis=str(raw.get("thesis", "")).strip(),
                provider=self._provider,
                model=resp.model,
            )
        except Exception as exc:
            logger.warning("%s decide() failed — abstaining: %s", self._provider, exc)
            return Decision(
                direction="neutral", conviction=0.0, confidence=0.0,
                thesis="", provider=self._provider,
                model=resolve_model(model, provider=self._provider), abstained=True,
            )

    async def _decide_jev(
        self, *, system: str, user: str,
        council_run_id: str | None, agent_decision_id: str | None,
        user_id: str | None,
    ) -> Decision:
        request = jev.build_request(system=system, user=user, model=_JEV_MODEL)
        # Billed on what we SENT, not on what came back: Jev returns no usage
        # block (see jev._CHARS_PER_TOKEN). Recorded before the parse so a
        # malformed answer is still a call we paid for — an abstain that costs
        # nothing in the ledger is an abstain nobody investigates.
        resp = LLMResponse(
            text="",
            model=_JEV_MODEL,
            input_tokens=jev.estimate_input_tokens(request),
            output_tokens=0,
        )
        try:
            body = await jev.call(
                request,
                api_key=self._api_key or "",
                endpoint=os.environ.get("JEV_ENDPOINT", jev.ENDPOINT).strip() or jev.ENDPOINT,
                timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
            )
        except jev.JevTransportError as exc:
            logger.warning("jev transport failed — abstaining: %s", exc)
            return Decision(
                direction="neutral", conviction=0.0, confidence=0.0, thesis="",
                provider="jev", model=_JEV_MODEL, abstained=True,
            )
        await _record_to_ledger(
            system, resp, is_mock=False,
            council_run_id=council_run_id,
            agent_decision_id=agent_decision_id,
            user_id=user_id,
        )
        try:
            d = jev.parse_response(body)
        except jev.JevContractError as exc:
            logger.warning("jev contract violation — abstaining: %s", exc)
            return Decision(
                direction="neutral", conviction=0.0, confidence=0.0, thesis="",
                provider="jev", model=_JEV_MODEL, abstained=True,
            )
        return Decision(
            direction=d.direction,
            conviction=d.conviction_0_1,
            confidence=d.confidence,
            # Jev generates no prose. Say so rather than leaving it blank —
            # a blank thesis in the approval inbox reads as a bug.
            thesis=_JEV_NO_THESIS,
            provider="jev",
            model=_JEV_MODEL,
        )

    @staticmethod
    def parse_json(text: str) -> dict[str, Any]:
        """Lenient JSON parse: fenced, bare, or embedded in prose.

        Stripping a leading fence is enough for Claude, which returns the
        object alone. GLM routinely wraps it: a sentence first, the object,
        then a note. Before this, that shape raised, `complete_json`
        re-asked (a second paid call), and a second prose reply degraded
        the node to its neutral fallback. That made the cheaper provider
        look MORE defensive for a formatting reason rather than a judgement
        one. Every node's JSON goes through here, so this is the one fix.

        When the reply holds several top-level objects, the LAST one wins.
        Models that reason before answering put the answer last, and an
        early `{...}` is far more often an example or a restated input than
        the verdict.

        Raises ``ValueError`` when no JSON object is present, so
        ``complete_json``'s re-ask and degraded path is unchanged.
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(json)?\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        decoder = json.JSONDecoder()
        found: dict[str, Any] | None = None
        i = cleaned.find("{")
        while i != -1:
            try:
                obj, end = decoder.raw_decode(cleaned, i)
            except json.JSONDecodeError:
                i = cleaned.find("{", i + 1)
                continue
            if isinstance(obj, dict):
                found = obj
            i = cleaned.find("{", end)
        if found is None:
            raise ValueError("no JSON object found in model output")
        return found



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
    council_run_id: str | None = None,
    agent_decision_id: str | None = None,
    user_id: str | None = None,
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
    # One Langfuse generation per agent node — the role (router / technical /
    # …) names it; level says succeeded / degraded / failed. No-op when
    # Langfuse keys are unset; never raises into the council.
    from trading_agents.tracing import agent_generation

    role = _infer_role(system)
    with agent_generation(role=role, model=model, system=system, user=user) as gen:
        resp = await llm.complete(
            system=system, user=user, model=model,
            max_tokens=max_tokens, cache_system=cache_system,
            council_run_id=council_run_id, agent_decision_id=agent_decision_id,
            user_id=user_id,
        )
        try:
            data = LLM.parse_json(resp.text)
            gen.succeed(output=data, usage=_usage_of(resp), cost=_cost_of(resp))
            return data, False
        except Exception as exc:  # malformed output, not a bug
            logger.warning("complete_json: parse failed (%s) — re-asking once", exc)

        retry_user = (
            f"{user}\n\nREMINDER: your previous reply was not valid JSON. "
            "Respond with the JSON object ONLY — no prose, no markdown fences."
        )
        resp = await llm.complete(
            system=system, user=retry_user, model=model,
            max_tokens=max_tokens, cache_system=cache_system,
            council_run_id=council_run_id, agent_decision_id=agent_decision_id,
            user_id=user_id,
        )
        try:
            data = LLM.parse_json(resp.text)
            gen.degrade(
                output=data,
                status="first reply was not valid JSON; retry parsed",
                usage=_usage_of(resp),
                cost=_cost_of(resp),
            )
            return data, True
        except Exception as exc:
            logger.error("complete_json: retry also malformed (%s) — degraded", exc)
            gen.fail(status="both attempts returned unparseable JSON", usage=_usage_of(resp))
            return None, True


def _infer_role(system: str) -> str:
    try:
        from trading_agents.cost_ledger import infer_role_from_system_prompt

        return infer_role_from_system_prompt(system)
    except Exception:
        return "unknown"


def _usage_of(resp: LLMResponse) -> dict[str, int]:
    return {"input": resp.input_tokens, "output": resp.output_tokens}


def _cost_of(resp: LLMResponse) -> float | None:
    try:
        from trading_agents.cost_ledger import compute_cost_usd

        return compute_cost_usd(
            model=resp.model,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cache_read_tokens=resp.cache_read_tokens,
            cache_creation_tokens=resp.cache_creation_tokens,
        )
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# Mock response generator — keyed on system-prompt keywords
# ─────────────────────────────────────────────────────────────────────


def _flatten(messages: list[dict[str, Any]]) -> str:
    """Pull one representative user-turn string out of a tool loop's
    ``messages`` list for ``_mock_response`` to key off of.

    The first turn is always ``{"role": "user", "content": <str>}`` (see
    ``llm_loop.run_tool_loop``); a later round instead appends a
    ``tool_result`` block list. Walk backward so the most recent user turn
    wins either way, and flatten a block list to one string.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(block.get("content", block)) if isinstance(block, dict) else str(block)
                for block in content
            ]
            return "\n".join(parts)
    return ""


def _extract_required_side(user: str) -> str:
    """The side the deterministic fit node fixed for this pass.

    The Drafter prompt states it explicitly, and the real Drafter node
    downgrades any contradicting verdict to HOLD. A mock that always says
    BUY would therefore make every short setup un-exercisable offline —
    the mock has to read the same constraint the model is given.
    """
    m = re.search(r"only non-HOLD verdict allowed is (BUY|SELL)", user)
    return m.group(1) if m else "BUY"


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
        side = _extract_required_side(user)
        short = side == "SELL"
        body = {
            "verdict": side,
            "confidence": 0.58,
            # A short's risk_level is higher for the same evidence — the
            # loss is unbounded. The mock mirrors that so the offline path
            # exercises the same shape the real prompt asks for.
            "risk_level": 4 if short else 2,
            "conviction_level": 3,
            "rationale": (
                f"MOCK: Council leans {'negative' if short else 'positive'} on {sym}. "
                f"Strategy fits the regime; {'shorting' if short else 'entering'} at market with an "
                "ATR-driven stop."
            ),
            "bull_case": (
                f"MOCK: the case FOR the short on {sym} — trend broken, price below both moving "
                "averages, momentum negative on a risk-adjusted basis."
                if short
                else f"Technical setup constructive on {sym}; momentum cluster aligns with sector strength. "
                "Pullback to 50-DMA already absorbed; volume confirms accumulation."
            ),
            "bear_case": (
                f"MOCK: what would squeeze the {sym} short — a sharp risk-on reversal, a short-interest "
                "squeeze, or an unmodelled catalyst. Borrow can be recalled."
                if short
                else f"If broader risk-off resumes, {sym} compresses fast. Insider activity flat, no insider tailwind. "
                "Earnings in two weeks add binary risk."
            ),
        }
    elif "you are the options bull agent" in role_line:
        body = {
            "direction": "long",
            "strategy": "momentum",
            "conviction": 0.6,
            "thesis": f"MOCK: {sym} breaks out within 3 weeks on volume expansion.",
        }
    elif "you are the options bear agent" in role_line:
        # Agrees with the Bull mock by default (same direction, conviction
        # within the 0.4 divergence band) — MOCK mode exists to let the
        # WHOLE pipeline run offline (module docstring above), including
        # the trade-hop path, the same way the equity mocks are tuned to
        # cooperate (Router picks all three analysts, the Drafter's mock
        # matches whatever side the prompt requires, etc.). A test that
        # needs the two agents to DISAGREE injects its own fake LLM rather
        # than relying on this shared generic mock for that scenario.
        body = {
            "direction": "long",
            "strategy": "momentum",
            "conviction": 0.55,
            "thesis": (
                f"MOCK: {sym} IV rank is unremarkable and liquidity clears the "
                "funnel, but re-check the spread at fill time within 3 weeks."
            ),
        }
    elif "you are the options escalation agent" in role_line:
        # Deliberately NEVER a tool call — see this function's own
        # docstring ("MOCK is TEXT ONLY") and options/escalation.py's
        # module docstring: this is the mechanism the fail-safe (§5.3)
        # relies on in MOCK mode. The body's shape doesn't matter to any
        # caller (nothing parses this as JSON — the escalation hop only
        # ever looks at `resp.tool_calls`, which stays empty for every
        # role), but HOLD is the honest canned answer for a role whose
        # entire job is "usually do nothing."
        body = {
            "action": "HOLD",
            "reason": (
                "MOCK: standing pat — the deterministic trailing ratchet is "
                "already managing this position every tick."
            ),
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


async def _record_to_ledger(
    system: str,
    resp: LLMResponse,
    *,
    is_mock: bool,
    council_run_id: str | None = None,
    agent_decision_id: str | None = None,
    user_id: str | None = None,
) -> None:
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
                agent_decision_id=agent_decision_id,
                user_id=user_id,
                council_run_id=council_run_id,
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
    except Exception as exc:
        logger.warning("cost ledger write failed (best-effort): %s", exc)
