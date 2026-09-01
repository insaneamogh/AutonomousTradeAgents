"""The analyst output ceiling must not be where replies get cut off.

Measured over 5 days of production `llm_calls`: the technical analyst's
`max_output_tokens` was 500 and its observed maximum was exactly 500 —
i.e. pinned at the cap, i.e. truncated mid-JSON. `complete_json` re-asks
once on unparseable output, so `calls/run` for that role sat at 1.70
against 1.02 for every role whose ceiling did not bind, and
`degraded_nodes` carried "technical" on 36 of 55 decisions.
"""

from __future__ import annotations

from trading_agents.nodes import _specialist


def test_the_ceiling_clears_the_largest_reply_seen_in_production() -> None:
    """418 avg / 500 max observed for `technical`, 559 max for the Drafter
    on a 900 ceiling. A ceiling at or near the observed max IS the bug —
    it is indistinguishable from truncation."""
    LARGEST_OBSERVED_ANALYST_REPLY = 559
    assert _specialist.MAX_TOKENS > LARGEST_OBSERVED_ANALYST_REPLY


def test_the_ceiling_is_not_back_at_the_truncating_value() -> None:
    """500 is the specific number that was cutting replies off. Pinned
    separately from the bound above so a future edit back to it fails
    loudly rather than passing on a technicality."""
    assert _specialist.MAX_TOKENS != 500


def test_the_analyst_ceiling_matches_the_drafters() -> None:
    """Both emit a verdict plus multi-sentence prose. There is no reason
    for the analyst — which runs on EVERY pass, unlike the Drafter — to
    have the tighter budget of the two."""
    from trading_agents.nodes.drafter import MAX_TOKENS as DRAFTER_MAX_TOKENS

    assert _specialist.MAX_TOKENS == DRAFTER_MAX_TOKENS
