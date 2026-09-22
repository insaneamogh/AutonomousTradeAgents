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
    assert resolve_model(Model.SONNET) == "glm-5.3"
    assert resolve_model(Model.HAIKU) == "glm-5.3-flash"


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
    """Z.ai revs GLM faster than we ship, so a stale id must be a Railway
    variable change rather than a code change."""
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_MODEL_SONNET", "glm-4.7")
    assert resolve_model(Model.SONNET) == "glm-4.7"
    assert resolve_model(Model.HAIKU) == "glm-5.3-flash", "other tiers untouched"


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


def test_every_default_glm_tier_is_priced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trap this closes has bitten twice (glm defaults, then Jev's id):
    change a default model id, forget the price row, and every call is billed
    at Sonnet's $3/$15. The $3/day spend cap then trips on phantom spend, and
    the A/B reports that the cheap provider saved nothing."""
    from trading_agents.cost_ledger import _PRICES

    monkeypatch.setenv("LLM_PROVIDER", "glm")
    for tier in (Model.OPUS, Model.SONNET, Model.HAIKU):
        monkeypatch.delenv(f"GLM_MODEL_{tier}", raising=False)
        wire = resolve_model(tier)
        assert wire in _PRICES, f"default GLM model {wire!r} for {tier} has no price row"


def test_glm_prices_are_the_published_list_prices() -> None:
    """docs.z.ai pricing, fetched 2026-09-23. glm-5.3 is $1.40 in / $4.40 out
    per million; glm-5.3-flash $0.15 / $0.50. A row that drifts below list
    price understates spend, which is the unsafe direction for a cap."""
    args = dict(input_tokens=1_000_000, output_tokens=200_000)
    assert compute_cost_usd(model="glm-5.3", **args) == pytest.approx(1.40 + 0.88)
    assert compute_cost_usd(model="glm-5.3-flash", **args) == pytest.approx(0.15 + 0.10)
    assert compute_cost_usd(model="glm-4.6", **args) == pytest.approx(0.60 + 0.44)


def test_glm_sends_its_key_as_bearer_and_never_the_anthropic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Z.ai documents its Anthropic-compatible endpoint with
    ANTHROPIC_AUTH_TOKEN (`Authorization: Bearer`). With `api_key` alone the
    SDK sends only `X-Api-Key`, which is not the documented scheme. Both
    headers must carry the GLM key, and the Anthropic key sitting in the
    environment must never reach Z.ai under either header."""
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
    headers = LLM()._get_client().auth_headers
    assert headers.get("Authorization") == "Bearer glm-test-key"
    assert headers.get("X-Api-Key") == "glm-test-key"
    assert not any("sk-ant" in str(v) for v in headers.values())


def test_anthropic_does_not_get_a_bearer_header_from_the_glm_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    headers = LLM()._get_client().auth_headers
    assert headers.get("X-Api-Key") == "sk-ant-test"
    assert "Authorization" not in headers
