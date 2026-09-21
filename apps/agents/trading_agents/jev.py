"""TypeSafe Jev — a structured-decision model, not a chat model.

Jev is ~143x cheaper than Sonnet on our workload ($0.042/M input, **output
free**) because it does not generate prose. You declare typed *questions*
and it returns typed *answers*:

    POST https://api.typesafe.ai/v1/systemone
    {"model": "jev-1.13.0",
     "state": {...},
     "questions": {"direction": {"type": "choice", "criteria": {...}},
                   "bullish_strength": {"type": "score", "criteria": [...]}}}

    -> {"answers": {"direction": {"type":"choice","choice":"bullish",
                                  "confidence":0.8,"probabilities":{...}},
                    "bullish_strength": {"type":"score","score":3,...}}}

**This is why it is NOT a drop-in for the Bull/Bear agents.** Those call
`open_option_trade` as a tool and write a `thesis` string; Jev does neither.
Of that tool's seven required arguments, `underlying` and `strategy` are
already known before the call, `take_profit_pct` / `stop_loss_pct` are
clamped by `effective_stop_loss_pct` regardless of what the model says, and
`direction` / `conviction` map exactly onto Jev's two question types. Only
`thesis` has no Jev equivalent — the reference implementation writes
literally "No written thesis generated."

So Jev can supply the decision-bearing half and deterministic code can
assemble the order. That is arguably the better shape (CLAUDE.md §3 — the
model influences *how much*, never composes the order), but it is a
behaviour change to the council and is deliberately NOT wired here. This
module ships the transport and the contract only; `LLM.decide()` is the
provider-neutral seam that calls it.

Contract mirrored from `virattt/ai-hedge-fund`'s working integration rather
than guessed, including the validation: probabilities must cover exactly
the expected keys and sum to 1.0, scores are 0-4, confidences are 0-1.
Anything else raises rather than being coerced — a malformed decision must
never quietly become a neutral one.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

logger = logging.getLogger("agents.jev")

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"

DIRECTIONS = ("bullish", "bearish", "neutral")
_SCORE_KEYS = {str(i) for i in range(5)}
_SCORE_MAX = 4
"""Jev scores are 0-4. The reference maps them to 0-100 conviction as
`score * 25`; we keep the raw score and let callers scale, so a change in
Jev's scale cannot silently rescale our conviction floor."""

_STRENGTH_RUBRIC = (
    "Evidence does not support the case or directly contradicts it.",
    "Limited support; substantial unsupported assumptions are required.",
    "Meaningful support with material conflicting evidence or unresolved gaps.",
    "Strong support across relevant criteria with limited material weaknesses.",
    "Compelling support across relevant criteria with no material contradiction "
    "apparent in the supplied evidence.",
)
"""The 0-4 score criteria, as full standards rather than the labels
"none/weak/moderate". A rubric of adjectives asks the model to pick a word;
this asks it to apply a test. Taken verbatim in spirit from the reference
integration, which is the only working calibration of this scale we have."""


class JevContractError(ValueError):
    """The response did not match the declared contract.

    Raised, never swallowed into a neutral answer: a parse failure and a
    genuine "no view" must not look the same to the caller. The caller
    decides whether that becomes an abstain.
    """


@dataclass(frozen=True)
class JevDecision:
    direction: str
    """One of DIRECTIONS."""

    strength: float
    """0-4 as Jev returns it. 0 when direction is neutral."""

    confidence: float
    """Jev's own 0-1 confidence in the DIRECTION answer. Deliberately kept
    separate from `strength`: one is "how sure the model is", the other is
    "how strong the case is", and collapsing them is how a confident read
    of a weak setup becomes a large position."""

    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def conviction_0_1(self) -> float:
        """Strength rescaled to the 0-1 the council's confidence floor uses."""
        return 0.0 if self.direction == "neutral" else self.strength / _SCORE_MAX


def build_request(
    *, system: str, user: str, model: str = MODEL, criteria: dict[str, str] | None = None
) -> dict[str, Any]:
    """One native Jev request.

    Each question is self-contained — the strength questions assess their
    own case and never assume the direction answer — so a single round trip
    returns a direction and both cases' strengths without the model
    rationalising one from the other.
    """
    base = criteria or {
        d: f"The evidence warrants the {d} assessment under the stated rules."
        for d in DIRECTIONS
    }
    return {
        "model": model,
        "state": {"instructions": system, "evidence": user},
        "questions": {
            "direction": {
                "type": "choice",
                "instructions": (
                    "Judge only from the supplied evidence. Which directional "
                    "assessment does it warrant?"
                ),
                "criteria": base,
            },
            **{
                f"{d}_strength": {
                    "type": "score",
                    "instructions": (
                        f"How strongly does the supplied evidence support the {d} "
                        "case? Assess the strength of the case, not your certainty "
                        "in the answer and not the probability of profit."
                    ),
                    "criteria": list(_STRENGTH_RUBRIC),
                }
                for d in ("bullish", "bearish")
            },
        },
    }


def _number(value: object, upper: float, path: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= upper
    ):
        raise JevContractError(f"{path} must be a finite number in [0, {upper:g}]")
    return float(value)


def _distribution(value: object, keys: set[str], path: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != keys:
        raise JevContractError(f"{path} must contain exactly {sorted(keys)}")
    out = {k: _number(v, 1, f"{path}.{k}") for k, v in value.items()}
    if not math.isclose(math.fsum(out.values()), 1.0, rel_tol=0.0, abs_tol=0.001):
        raise JevContractError(f"{path} must sum to 1.0 within 0.001")
    return out


def parse_response(response: object) -> JevDecision:
    """Validate and normalise. Raises `JevContractError` on anything off.

    Strict on purpose. A silently-coerced malformed answer is worse than an
    error here: it becomes a real position sized by a number nobody checked.
    """
    if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        raise JevContractError("response.answers must be an object")
    answers = response["answers"]

    for name, want in (
        ("direction", "choice"),
        ("bullish_strength", "score"),
        ("bearish_strength", "score"),
    ):
        a = answers.get(name)
        if not isinstance(a, dict) or a.get("type") != want:
            raise JevContractError(f"{name} must be a {want} answer")
        _number(a.get("confidence"), 1, f"{name}.confidence")
        _distribution(
            a.get("probabilities"),
            set(DIRECTIONS) if want == "choice" else _SCORE_KEYS,
            f"{name}.probabilities",
        )
        if want == "score":
            _number(a.get("score"), _SCORE_MAX, f"{name}.score")

    direction = answers["direction"].get("choice")
    if direction not in DIRECTIONS:
        raise JevContractError(f"direction.choice must be one of {DIRECTIONS}")

    strength = (
        0.0
        if direction == "neutral"
        else float(answers[f"{direction}_strength"]["score"])
    )
    return JevDecision(
        direction=direction,
        strength=strength,
        confidence=float(answers["direction"]["confidence"]),
        probabilities=dict(answers["direction"]["probabilities"]),
    )


# ─────────────────────────────────────────────────────────────────────
# Transport
#
# Deliberately NOT the Anthropic SDK. Jev is not wire-compatible with it:
# there is no `messages`, no `max_tokens`, no content blocks and no
# streaming. It is one POST of a typed question set, one JSON answer set.
# ─────────────────────────────────────────────────────────────────────

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
"""Retried once. Taken from the reference integration rather than from a
generic "5xx is retryable" instinct — 529 in particular is an overload code
that a naive list would miss."""

_CHARS_PER_TOKEN = 4.0
"""Jev's response carries **no usage block** — verified against the
reference integration, which reads none because there is none to read. So
the cost ledger cannot be told what we were billed; it has to be told what
we sent. This is the standard ~4-chars-per-token approximation applied to
the serialised request.

Stated plainly: **Jev rows in the cost ledger are ESTIMATES, not receipts.**
Every other provider's rows are exact. At $0.042/M the absolute error is
cents on a year of trading, but it is still an estimate and the ledger must
not imply otherwise."""


class JevTransportError(RuntimeError):
    """The call did not complete. Distinct from `JevContractError`:
    that one means Jev answered and the answer was malformed, this one
    means we never got a valid answer at all. Callers that abstain on
    model failure need to tell those apart from a data-layer failure."""


def estimate_input_tokens(request: dict[str, Any]) -> int:
    """Approximate billable input for one request. See `_CHARS_PER_TOKEN`."""
    return max(1, int(len(json.dumps(request, separators=(",", ":"))) / _CHARS_PER_TOKEN))


def redact(value: Any, api_key: str) -> Any:
    """Strip the key out of anything we are about to log or raise.

    Not paranoia: an upstream error body can echo the Authorization header
    back, and this object ends up in exception messages and log lines. The
    reference integration does the same, and for the same reason.
    """
    if not api_key:
        return value
    if isinstance(value, str):
        return value.replace(api_key, "[REDACTED]")
    if isinstance(value, dict):
        return {redact(k, api_key): redact(v, api_key) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, api_key) for v in value]
    return value


def retry_delay_seconds(header: str | None, *, now: datetime | None = None) -> float:
    """`Retry-After` as seconds. Accepts delta-seconds or an HTTP-date.

    Floors at 1.0 so a malformed or already-past header cannot turn the
    single retry into a hot loop.
    """
    if header is None:
        return 1.0
    try:
        seconds = float(header)
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError, OverflowError):
            return 1.0
        if when is None:
            return 1.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if not math.isfinite(seconds):
        return 1.0
    return max(1.0, seconds)


async def call(
    request: dict[str, Any],
    *,
    api_key: str,
    endpoint: str = ENDPOINT,
    timeout: float = 60.0,  # noqa: ASYNC109 - httpx's own timeout, see below
) -> dict[str, Any]:
    """POST one typed question set. Returns the raw decoded body.

    Raises `JevTransportError` on anything that is not a decoded 2xx JSON
    object. Parsing into a `JevDecision` is the caller's next step, so a
    contract failure stays distinguishable from a transport failure.

    `timeout` is httpx's, not `asyncio.timeout`'s, on purpose: httpx applies
    it per connect / read / write, so a slow-but-progressing response is not
    killed the way a single wall-clock budget would kill it. The retry sleep
    is deliberately OUTSIDE that budget, which is why the `Retry-After` check
    below compares against it explicitly.
    """
    if not api_key or not api_key.strip():
        raise JevTransportError("TYPESAFE_API_KEY is empty")

    import asyncio

    import httpx

    last: str = "no attempt completed"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        # `follow_redirects=False` is a security control, not a default.
        # httpx forwards the Authorization header across a redirect, so an
        # upstream 302 to another host would hand our key to that host.
        for attempt in (1, 2):
            try:
                resp = await client.post(
                    endpoint,
                    json=request,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.HTTPError as exc:
                # Do NOT chain: httpx exception reprs can carry the request,
                # and the request carries the header.
                last = f"transport failure: {type(exc).__name__}"
                if attempt == 1:
                    await asyncio.sleep(1.0)
                    continue
                raise JevTransportError(last) from None

            if resp.status_code in _RETRY_STATUSES and attempt == 1:
                delay = retry_delay_seconds(resp.headers.get("Retry-After"))
                if delay > timeout:
                    raise JevTransportError(
                        f"HTTP {resp.status_code}: Retry-After {delay:g}s exceeds "
                        f"the {timeout:g}s timeout"
                    ) from None
                await asyncio.sleep(delay)
                continue

            if not 200 <= resp.status_code < 300:
                raise JevTransportError(
                    redact(f"HTTP {resp.status_code}: {resp.text[:400]}", api_key)
                ) from None

            try:
                body = resp.json()
            except ValueError:
                raise JevTransportError("response was not valid JSON") from None
            if not isinstance(body, dict):
                raise JevTransportError("response was not a JSON object") from None
            return body

    raise JevTransportError(last)
