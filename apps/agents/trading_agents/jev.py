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
module ships the transport and the contract only.

Contract mirrored from `virattt/ai-hedge-fund`'s working integration rather
than guessed, including the validation: probabilities must cover exactly
the expected keys and sum to 1.0, scores are 0-4, confidences are 0-1.
Anything else raises rather than being coerced — a malformed decision must
never quietly become a neutral one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13"

DIRECTIONS = ("bullish", "bearish", "neutral")
_SCORE_KEYS = {str(i) for i in range(5)}
_SCORE_MAX = 4
"""Jev scores are 0-4. The reference maps them to 0-100 conviction as
`score * 25`; we keep the raw score and let callers scale, so a change in
Jev's scale cannot silently rescale our conviction floor."""


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
                    "criteria": ["none", "weak", "moderate", "strong", "very strong"],
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
