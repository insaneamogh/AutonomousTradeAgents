"""TypeSafe Jev — transport contract, routing, and pricing.

Jev is ~143x cheaper than Sonnet on our workload ($0.042/M input, output
free) because it does not generate prose. You declare typed questions and
it returns typed answers.

**It is not a drop-in for the Bull/Bear agents**, and the tests here do not
pretend otherwise — those agents call `open_option_trade` as a tool and
write a `thesis` string, and Jev does neither. This ships the transport and
the contract; wiring the council to it is a behaviour change with its own
A/B.

The live endpoint is UNVERIFIED — there is no TYPESAFE_API_KEY on this
machine, so everything below exercises the request shape, the response
contract and the routing, not a real round trip. The contract is mirrored
from `virattt/ai-hedge-fund`'s working integration rather than guessed.
"""

from __future__ import annotations

import pytest

from trading_agents.cost_ledger import compute_cost_usd
from trading_agents.jev import (
    DIRECTIONS,
    JevContractError,
    build_request,
    parse_response,
)
from trading_agents.llm import LLM, Model, active_provider, resolve_model


def _answers(direction: str = "bullish", score: int = 3) -> dict:
    even = {str(i): 0.2 for i in range(5)}
    return {
        "answers": {
            "direction": {
                "type": "choice", "choice": direction, "confidence": 0.8,
                "probabilities": {"bullish": 0.7, "bearish": 0.2, "neutral": 0.1},
            },
            "bullish_strength": {
                "type": "score", "score": score, "confidence": 0.7,
                "probabilities": dict(even),
            },
            "bearish_strength": {
                "type": "score", "score": 1, "confidence": 0.6,
                "probabilities": dict(even),
            },
        }
    }


# ── the request ───────────────────────────────────────────────────────


def test_request_asks_all_three_questions_independently() -> None:
    """Each question is self-contained so the strength answers cannot be
    rationalised from the direction answer."""
    r = build_request(system="rules", user="evidence")
    assert set(r["questions"]) == {"direction", "bullish_strength", "bearish_strength"}
    assert r["questions"]["direction"]["type"] == "choice"
    assert r["questions"]["bullish_strength"]["type"] == "score"


# ── the response contract ─────────────────────────────────────────────


def test_parses_a_well_formed_answer() -> None:
    d = parse_response(_answers("bullish", 3))
    assert d.direction == "bullish"
    assert d.strength == 3.0
    assert d.conviction_0_1 == 0.75


def test_neutral_carries_no_conviction() -> None:
    assert parse_response(_answers("neutral")).conviction_0_1 == 0.0


def test_confidence_and_strength_stay_separate() -> None:
    """One is "how sure the model is", the other is "how strong the case
    is". Collapsing them is how a confident read of a weak setup becomes a
    large position."""
    d = parse_response(_answers("bullish", score=0))
    assert d.confidence == 0.8
    assert d.strength == 0.0
    assert d.conviction_0_1 == 0.0


@pytest.mark.parametrize(
    "mutate,label",
    [
        (lambda r: r.pop("answers"), "no answers"),
        (lambda r: r["answers"]["direction"].__setitem__("choice", "up"), "bad choice"),
        (lambda r: r["answers"]["direction"].__setitem__("confidence", 1.5), "confidence > 1"),
        (lambda r: r["answers"]["bullish_strength"].__setitem__("score", 9), "score > 4"),
        (lambda r: r["answers"]["direction"].__setitem__(
            "probabilities", {"bullish": 0.9, "bearish": 0.9, "neutral": 0.9}), "probs sum != 1"),
        (lambda r: r["answers"]["direction"].__setitem__("type", "score"), "wrong answer type"),
    ],
)
def test_a_malformed_answer_raises_rather_than_becoming_neutral(mutate, label) -> None:
    """Strict on purpose. A silently-coerced malformed answer is worse than
    an error: it becomes a real position sized by a number nobody checked,
    and a parse failure must never look like a genuine "no view"."""
    r = _answers()
    mutate(r)
    with pytest.raises(JevContractError):
        parse_response(r)


def test_every_direction_round_trips() -> None:
    for d in DIRECTIONS:
        assert parse_response(_answers(d)).direction == d


# ── routing ───────────────────────────────────────────────────────────


def test_jev_is_opt_in_and_a_typo_cannot_break_the_desk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert active_provider() == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "jev")
    assert active_provider() == "jev"
    assert resolve_model(Model.SONNET) == "jev-1.13"
    monkeypatch.setenv("LLM_PROVIDER", "jevv")
    assert active_provider() == "anthropic"


def test_jev_reads_its_own_key_and_never_the_anthropic_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting a provider must never spend a different provider's key —
    and the cheap provider has to be reachable WITHOUT restoring the
    Anthropic key, which is the situation we are actually in."""
    monkeypatch.setenv("LLM_PROVIDER", "jev")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-looking")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert LLM().mock is True, "an Anthropic key must not enable Jev"

    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key")
    assert LLM().mock is False


# ── pricing ───────────────────────────────────────────────────────────


def test_jev_output_is_free_not_unpriced() -> None:
    """The zeros in the price row are load-bearing. If a future cleanup
    decides "0 must mean unpriced" and falls back to Sonnet's rates, Jev
    silently starts being billed $15/M on output it does not charge for."""
    in_only = compute_cost_usd(model="jev-1.13", input_tokens=1_000_000, output_tokens=0)
    huge_out = compute_cost_usd(
        model="jev-1.13", input_tokens=1_000_000, output_tokens=5_000_000
    )
    assert in_only == huge_out
    assert in_only == pytest.approx(0.042, rel=1e-6)


def test_jev_is_far_cheaper_than_the_alternatives() -> None:
    a = dict(input_tokens=1_000_000, output_tokens=200_000)
    jev = compute_cost_usd(model="jev-1.13", **a)
    assert jev < compute_cost_usd(model="glm-4.6", **a) / 10
    assert jev < compute_cost_usd(model=Model.SONNET, **a) / 100
