"""Pins the analyst-prompt calibration fix.

Live measurement (docs/PLAN_NEXT.md §0.45, docs/OPTIONS_PLAN.md's triage
notes): the deterministic ``strategy_fit`` layer scored SPY/QQQ/NVDA/AAPL
0.65-0.88 against a 0.42 floor, while the LLM analysts independently scored
the SAME names 28-42 out of 100 against ``min_specialist_avg_score``'s
40-45 floor. No local ``ANTHROPIC_API_KEY`` was available to re-run that
live comparison from this checkout (see the build-log entry for this
commit) — the root cause below was established by reading the actual
prompt templates, not by re-observing the 28-42 scores directly.

Root cause, reading ``prompts/{fundamental,macro,technical}_analyst.py`` as
they stood before this fix: every one of the three enumerated multiple
explicit "score DOWN when X" heuristics (weak quality_score, VIX > 30,
rising yields into a rate-sensitive name, a strong dollar, >15% below the
200DMA, RSI > 75) and, between all three prompts, exactly ONE explicit
"score UP" trigger (macro's "sector RS positive AND regime=bull"). Combined
with "Honesty over enthusiasm" / "Be honest, if X is weak say so" / "lean
neutral" framing repeated in all three, the rubric gave a model many named
reasons to mark down and almost none to mark up — priming the output
distribution toward the low half of 0-100 independent of the underlying
evidence. This compounds with a structural mismatch: ``strategy_fit`` takes
the BEST of 5 independently-lenient strategies (generous ramps, missing
data defaults to NEUTRAL=0.5), while the specialist-average rule MEANS three
independently-skeptical single-shot judgments — a max-of-lenient will read
higher than a mean-of-skeptical for identical evidence even with a perfectly
symmetric prompt, but the asymmetric enumeration made it categorically
worse. Reproduced directly against ``strategies.fit.best_strategy`` (not
live, but real code): a deliberately UNREMARKABLE, no-extremes "boring
uptrend" feature dict (2% above the 20DMA, 3% above the 50DMA, RSI 58,
Sharpe 0.3, realized vol 18%, average volume) — the kind of tape the OLD
prompts' own anchor language ("lean neutral", "don't reach for extremes")
would calibrate a human-like analyst to call ~50 — scores 0.854 via
``sma_crossover``, because ``trend_regime_aligned`` alone maxes out at 1.0
(weight 0.35) from "uptrend" being true, and the DMA-distance ramps
(``price_vs_20dma``/``price_vs_50dma``) saturate at only +3%/+5%.

This file does not (cannot, offline) assert on a real LLM's output score.
It pins the two concrete, checkable properties of the fix instead: the
asymmetric down-only language is gone, and a same-scale, explicit "score
up"/anchor-band vocabulary is present — so a future prompt edit cannot
silently reintroduce the asymmetry without this test's regex assertions
failing. Revert either prompt file to its pre-fix wording and every
assertion in this file fails (verified via CLAUDE.md §4.1 revert-check
during development, then restored).
"""

from __future__ import annotations

import re

from trading_agents.prompts import FUNDAMENTAL_ANALYST, MACRO_ANALYST, TECHNICAL_ANALYST

_ANALYST_PROMPTS = {
    "fundamental": FUNDAMENTAL_ANALYST,
    "macro": MACRO_ANALYST,
    "technical": TECHNICAL_ANALYST,
}

# The exact asymmetric, down-only framing that shipped before this fix.
# Any of these reappearing is the calibration bug coming back.
_BANNED_PHRASES = (
    "honesty over enthusiasm",
    "lean neutral",
)

# A "this is what a genuinely good setup looks like" anchor must exist
# somewhere in the 60s-80s band — not just a neutral-50 anchor and a set
# of penalty triggers.
_GOOD_SETUP_ANCHOR = re.compile(r"6[0-9]-8[0-9]|65-84|65-80|65-85")

# An explicit instruction to score UP, symmetric with the (legitimate,
# kept) "score down" / "flag" heuristics.
_SCORE_UP_INSTRUCTION = re.compile(r"score(?:d| it)? up|score up|scoring up", re.IGNORECASE)


def test_no_analyst_prompt_uses_the_old_asymmetric_pessimism_framing() -> None:
    for name, prompt in _ANALYST_PROMPTS.items():
        lowered = prompt.lower()
        for phrase in _BANNED_PHRASES:
            assert phrase not in lowered, (
                f"{name} analyst prompt still contains the old down-only framing "
                f"{phrase!r} — this is the exact language the calibration bug traced "
                "back to (see module docstring)."
            )


def test_every_analyst_prompt_anchors_the_good_setup_range() -> None:
    """Each prompt must name a concrete 60s-80s band as the ordinary
    "good, tradeable" range — not just a neutral-50 default and a list of
    penalties. Without this anchor a model has no positive reference point
    anywhere above "unremarkable"."""
    for name, prompt in _ANALYST_PROMPTS.items():
        assert _GOOD_SETUP_ANCHOR.search(prompt), (
            f"{name} analyst prompt does not anchor a 60s-80s 'genuinely good "
            "setup' band — see the calibration anchors this fix added."
        )


def test_every_analyst_prompt_has_an_explicit_score_up_instruction() -> None:
    """Symmetry check: a model told only when to mark DOWN will drift low
    regardless of the evidence. Each prompt must carry at least one
    explicit "score up" instruction to match its "score down" / "flag"
    heuristics."""
    for name, prompt in _ANALYST_PROMPTS.items():
        assert _SCORE_UP_INSTRUCTION.search(prompt), (
            f"{name} analyst prompt has no explicit 'score up' instruction — "
            "an enumerated-penalties-only rubric is exactly the asymmetry "
            "this fix corrects."
        )


def test_confidence_and_score_are_explicitly_decoupled() -> None:
    """The old wording conflated "thin evidence -> low confidence" with
    "thin evidence -> low SCORE" (both fundamental and technical said
    "lean neutral" / pulled toward 50 under low confidence). Confidence
    and score must be named as separate axes so a low-confidence read of
    strong evidence doesn't get dragged toward 50."""
    for name, prompt in _ANALYST_PROMPTS.items():
        assert "separate axis" in prompt.lower(), (
            f"{name} analyst prompt no longer states that confidence is a "
            "separate axis from score."
        )
