"""Drift check: every ``veto_rule`` identifier the risk engine can actually
emit must have a label in the shared, canonical map.

Deliberately a static text scan of the rule source files rather than an
import + exercise of the rule functions — matches this repo's preference
for cheap, direct checks over assembling the full RiskContext/RiskProposal
fixtures just to enumerate string literals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

RULES_DIR = Path(__file__).resolve().parents[1] / "engine" / "risk" / "rules"
LIVE_TRADING_GATE = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "api"
    / "app"
    / "services"
    / "orders"
    / "live_trading_gate.py"
)
LABELS_JSON = Path(__file__).resolve().parents[2] / "shared-types" / "src" / "vetoRuleLabels.json"

# `\b` before `veto_rule` deliberately excludes `risk_veto_rule=` (the two
# are joined by `_`, a word character, so there is no boundary there) —
# that literal is scanned separately below, matching the two genuinely
# different call sites (the rules package vs. the live-trading gate).
_VETO_RULE_RE = re.compile(r'\bveto_rule\s*=\s*"([^"]+)"')
_RISK_VETO_RULE_RE = re.compile(r'\brisk_veto_rule\s*=\s*"([^"]+)"')


def _required_rule_identifiers() -> set[str]:
    """Every ``veto_rule=`` / ``risk_veto_rule=`` string literal the engine
    can actually emit, found by scanning source text."""
    found: set[str] = set()
    for path in sorted(RULES_DIR.glob("*.py")):
        found.update(_VETO_RULE_RE.findall(path.read_text(encoding="utf-8")))
    found.update(_RISK_VETO_RULE_RE.findall(LIVE_TRADING_GATE.read_text(encoding="utf-8")))
    return found


def test_shared_label_map_covers_every_required_veto_rule() -> None:
    """packages/shared-types/src/vetoRuleLabels.json must have a label for
    every identifier the engine can emit. A superset is fine — e.g. the
    defensive ``unnamed_rule`` entry — so this checks subset, not equality.
    """
    required = _required_rule_identifiers()
    # Guards against a silently-empty scan (e.g. a moved rules dir) making
    # the subset assertion below vacuously true.
    assert len(required) >= 20, (
        f"expected at least 20 rule identifiers from {RULES_DIR} + "
        f"{LIVE_TRADING_GATE.name}, found {len(required)}: {sorted(required)}"
    )

    labels = json.loads(LABELS_JSON.read_text(encoding="utf-8"))

    missing = required - labels.keys()
    assert not missing, (
        f"vetoRuleLabels.json is missing labels for: {sorted(missing)} — "
        "every veto_rule the engine can emit needs a human label there."
    )
