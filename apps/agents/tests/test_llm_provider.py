"""Provider routing — Anthropic by default, GLM opt-in.

The council's Sonnet calls were 88% of this account's LLM spend ($9.49 of
$10.74), which is what led the operator to pull the API key and stop the
desk. Z.ai serves an Anthropic-compatible endpoint, so GLM is a base-URL
swap: same SDK, same tool-calling shape, no caller changes.

This exists to be A/B'd, not flipped on and forgotten — the council's job
is judgement, and a cheaper model that abstains more costs far more in bad
trades than it saves in tokens.
"""

from __future__ import annotations

import logging

import pytest

from trading_agents.cost_ledger import compute_cost_usd
from trading_agents.llm import LLM, Model, active_provider, resolve_model


def test_anthropic_is_the_default() -> None:
    assert active_provider() == "anthropic"
    assert resolve_model(Model.SONNET) == Model.SONNET


def test_glm_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    assert active_provider() == "glm"
    assert resolve_model(Model.SONNET) == "glm-4.6"
    assert resolve_model(Model.HAIKU) == "glm-4.5-air"


def test_an_unknown_provider_falls_back_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in an env var must not take the desk down."""
    monkeypatch.setenv("LLM_PROVIDER", "gpt4")
    assert active_provider() == "anthropic"
    assert resolve_model(Model.SONNET) == Model.SONNET


def test_an_unmapped_model_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Better to send a name the provider may accept than to silently
    substitute a different model than the caller asked for."""
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    assert resolve_model("some-future-model") == "some-future-model"


def test_glm_reads_its_own_key_not_the_anthropic_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two are independent on purpose: the operator removed
    ANTHROPIC_API_KEY to stop the spend, so the cheaper provider has to be
    reachable without putting it back — and pointing at GLM must never
    spend an Anthropic key."""
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    llm = LLM()
    assert llm.provider == "glm"
    assert llm.mock is False, "a present GLM key must enable real mode"


def test_no_glm_key_means_mock_even_with_an_anthropic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-looking")
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    assert LLM().mock is True


def test_the_base_url_is_only_set_for_glm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entire integration is this one kwarg. If it stops being passed,
    GLM traffic silently goes to Anthropic on an Anthropic key."""
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    client = LLM()._get_client()
    assert "z.ai" in str(client.base_url)

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert "z.ai" not in str(LLM()._get_client().base_url)


def test_glm_is_priced_and_not_billed_at_sonnet_rates() -> None:
    """Without price rows the ledger falls back to Sonnet and reports a
    saving that never happened — worse than no saving, because the point of
    the option is to MEASURE whether the cheaper model is worth it."""
    args = dict(input_tokens=1_000_000, output_tokens=200_000)
    sonnet = compute_cost_usd(model=Model.SONNET, **args)
    glm = compute_cost_usd(model="glm-4.6", **args)
    assert glm < sonnet / 5, f"glm {glm} vs sonnet {sonnet}"
    assert glm > 0, "a priced model must never cost nothing"


def test_a_cache_hit_on_glm_is_not_free() -> None:
    assert compute_cost_usd(
        model="glm-4.6", input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000,
    ) > 0


def test_a_glm_tier_can_be_repointed_without_a_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Z.ai revs GLM faster than we ship. Their docs already advertise
    GLM-5.3 while our table pins 4.6, so a stale id must be a Railway
    variable change rather than a code change."""
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_MODEL_SONNET", "glm-5.3")
    assert resolve_model(Model.SONNET) == "glm-5.3"
    assert resolve_model(Model.HAIKU) == "glm-4.5-air", "other tiers untouched"


def test_an_override_does_not_leak_into_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GLM_MODEL_SONNET", "glm-5.3")
    assert resolve_model(Model.SONNET) == Model.SONNET


def test_an_unpriced_model_warns_instead_of_silently_billing_as_sonnet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure this guards was real: the Jev row was keyed `jev-1.13`
    while the model id is `jev-1.13.0`, so every Jev call would have been
    priced at Sonnet's $3/$15 — on the provider chosen precisely BECAUSE it
    is ~140x cheaper. Silent fallback makes a cost migration unmeasurable."""
    from trading_agents.cost_ledger import _warned_unpriced, compute_cost_usd

    _warned_unpriced.discard("not-a-real-model")
    with caplog.at_level(logging.WARNING, logger="agents.cost"):
        compute_cost_usd(model="not-a-real-model", input_tokens=1000, output_tokens=10)
    assert any("no price row" in r.getMessage() for r in caplog.records)


def test_the_unpriced_warning_does_not_repeat_per_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It is on the hot path of every LLM call; a per-call warning buries the
    signal it exists to raise."""
    from trading_agents.cost_ledger import _warned_unpriced, compute_cost_usd

    _warned_unpriced.discard("noisy-model")
    with caplog.at_level(logging.WARNING, logger="agents.cost"):
        for _ in range(5):
            compute_cost_usd(model="noisy-model", input_tokens=1000, output_tokens=10)
    hits = [r for r in caplog.records if "no price row" in (r.getMessage())]
    assert len(hits) == 1
