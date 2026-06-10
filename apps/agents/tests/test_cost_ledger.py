"""LLM cost-ledger tests.

  - ``compute_cost_usd`` is pure — golden-number checks per model.
  - ``InMemoryCostLedger.sum_cost_since`` excludes mocks by default.
  - ``LLM.complete`` writes a row on every call (mock + real). Tested
    via the mock path; real path is covered by the existing opt-in
    real-LLM smoke.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from trading_agents.cost_ledger import (
    InMemoryCostLedger,
    LedgerEntry,
    compute_cost_usd,
    get_cost_ledger,
    infer_role_from_system_prompt,
    reset_cost_ledger_for_tests,
)
from trading_agents.llm import LLM


@pytest.fixture(autouse=True)
def _reset_ledger() -> None:
    reset_cost_ledger_for_tests()


# ─────────────────────────────────────────────────────────────────────
# Pricing math
# ─────────────────────────────────────────────────────────────────────


def test_compute_cost_haiku_input_only() -> None:
    """Haiku: $1/M input → 1000 input tokens = $0.001."""
    cost = compute_cost_usd(
        model="claude-haiku-4-5-20251001",
        input_tokens=1_000,
        output_tokens=0,
    )
    assert cost == pytest.approx(0.001, abs=1e-6)


def test_compute_cost_sonnet_in_and_out() -> None:
    """Sonnet: $3/M input + $15/M output."""
    cost = compute_cost_usd(
        model="claude-sonnet-4-6",
        input_tokens=2_000,
        output_tokens=500,
    )
    # 2000 * 3 + 500 * 15 = 6000 + 7500 = 13500 → $0.0135
    assert cost == pytest.approx(0.0135, abs=1e-6)


def test_compute_cost_with_cache_reads() -> None:
    """Cache reads charge at 10% of input (Anthropic convention)."""
    cost = compute_cost_usd(
        model="claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=10_000,
    )
    # Sonnet cache read = $0.30/M → 10_000 * 0.30 / 1_000_000 = $0.003
    assert cost == pytest.approx(0.003, abs=1e-6)


def test_compute_cost_strips_mock_suffix() -> None:
    """``LLM._mock_response`` returns models suffixed with ``+mock`` —
    the cost calc must strip the suffix before looking up the price.
    """
    real = compute_cost_usd(
        model="claude-haiku-4-5-20251001",
        input_tokens=1_000,
        output_tokens=0,
    )
    suffixed = compute_cost_usd(
        model="claude-haiku-4-5-20251001+mock",
        input_tokens=1_000,
        output_tokens=0,
    )
    assert real == suffixed


def test_compute_cost_unknown_model_falls_back_to_sonnet() -> None:
    cost = compute_cost_usd(
        model="claude-future-99",
        input_tokens=1_000,
        output_tokens=0,
    )
    assert cost == pytest.approx(0.003, abs=1e-6)  # Sonnet input rate


def test_compute_cost_clamps_negative_inputs() -> None:
    assert compute_cost_usd(
        model="claude-sonnet-4-6", input_tokens=-1_000, output_tokens=0
    ) == 0.0


# ─────────────────────────────────────────────────────────────────────
# Role inference
# ─────────────────────────────────────────────────────────────────────


def test_infer_role_router() -> None:
    assert infer_role_from_system_prompt("You are the Router on a quant desk.") == "router"


def test_infer_role_strategy_selector() -> None:
    assert (
        infer_role_from_system_prompt("You are the Strategy Selector — pick one id.")
        == "selector"
    )


def test_infer_role_unknown() -> None:
    assert infer_role_from_system_prompt("Hello there.") == "unknown"


# ─────────────────────────────────────────────────────────────────────
# Sum + mock exclusion
# ─────────────────────────────────────────────────────────────────────


async def test_ledger_sum_excludes_mocks_by_default() -> None:
    ledger = InMemoryCostLedger()
    await ledger.record(
        LedgerEntry(model="claude-haiku-4-5-20251001", cost_usd=0.50, is_mock=False)
    )
    await ledger.record(
        LedgerEntry(model="claude-haiku-4-5-20251001", cost_usd=0.30, is_mock=True)
    )
    total, n = await ledger.sum_cost_since(timedelta(days=1), exclude_mock=True)
    assert total == pytest.approx(0.50)
    assert n == 1

    total_all, n_all = await ledger.sum_cost_since(timedelta(days=1), exclude_mock=False)
    assert total_all == pytest.approx(0.80)
    assert n_all == 2


# ─────────────────────────────────────────────────────────────────────
# LLM wrapper writes a row
# ─────────────────────────────────────────────────────────────────────


async def test_llm_complete_writes_to_ledger() -> None:
    llm = LLM(api_key=None)  # mock mode
    assert llm.mock is True

    await llm.complete(
        system="You are the Router on a quant desk.",
        user="Ticker: NVDA",
        model="claude-haiku-4-5-20251001",
    )

    rows = await get_cost_ledger().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.role == "router"
    assert row.is_mock is True
    # Mock responses don't report token usage, so cost should be 0.
    assert row.cost_usd == 0.0
