"""The shared body of the three specialist analyst nodes.

Technical, Fundamental and Macro are the same node three times: render a
feature dict into an aligned prompt, ask one model for
``{score, confidence, thesis, citations}``, clamp what comes back, and
record the node in ``degraded_nodes`` if the call fell over.

They used to be three copy-pasted files. The duplication mattered because
the *degradation* contract lived in it: "a parse failure becomes a neutral
50/0.2, never an exception, and the node names itself as degraded so the
Risk Officer can see the council was running blind." Three copies is three
chances for one analyst to quietly stop reporting itself as degraded.

So the contract lives here once, and each analyst file is reduced to what
actually differs between them: which features it reads, how it labels
them, which prompt, and which model tier.
"""

from __future__ import annotations

import logging

from trading_agents.llm import LLM, Model, complete_json
from trading_agents.nodes._guards import clamp_confidence, clamp_score
from trading_agents.state import CouncilState

#: What an analyst reports when its LLM call produced nothing usable.
#: Neutral score, low-but-nonzero confidence — the council keeps running
#: with this analyst contributing almost no weight, rather than crashing.
NEUTRAL_ON_PARSE_ERROR = {
    "score": 50.0,
    "confidence": 0.2,
    "thesis": "Parse error — neutral default.",
    "citations": [],
}

MAX_TOKENS = 500


def render_features(
    features: dict[str, object], keys: tuple[str, ...], *, label_width: int
) -> str:
    """Render ``keys`` from ``features`` as an aligned ``  name:   value`` block.

    Missing keys render as ``n/a`` rather than being dropped, so the model
    can tell "this feature is unavailable" apart from "this feature was
    never part of my brief". ``label_width`` is per-analyst and fixed:
    changing the alignment rewrites the prompt and busts its cache entry.
    """
    return "".join(
        f"  {key + ':':<{label_width}}{features.get(key, 'n/a')}\n" for key in keys
    )


async def run_specialist(
    state: CouncilState,
    llm: LLM,
    *,
    name: str,
    system: str,
    model: Model,
    header: str,
    body: str,
) -> CouncilState:
    """Run one specialist analyst and merge its verdict into ``state``.

    ``name`` is the analyst's identity everywhere it is observable: the key
    its verdict lands under on state, the label in ``degraded_nodes``, and
    the ``field`` prefix the clamp warnings log under.
    """
    data, degraded = await complete_json(
        llm, system=system, user=header + body, model=model, max_tokens=MAX_TOKENS
    )
    if data is None:
        # Same per-analyst log channel the three separate modules used, so
        # existing log filters on agents.node.technical keep working.
        logging.getLogger(f"agents.node.{name}").warning(
            "%s degraded — neutral default", name
        )
        data = dict(NEUTRAL_ON_PARSE_ERROR)

    degraded_nodes = list(state.get("degraded_nodes") or [])
    if degraded:
        degraded_nodes.append(name)

    return {
        **state,
        name: {
            "score": clamp_score(data.get("score", 50.0), field=f"{name}.score"),
            "confidence": clamp_confidence(
                data.get("confidence", 0.0), field=f"{name}.confidence"
            ),
            "thesis": str(data.get("thesis", "")),
            "citations": list(data.get("citations", [])),
        },
        "degraded_nodes": degraded_nodes,
    }
