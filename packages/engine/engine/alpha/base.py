"""`AlphaModel` -> `Signal`: one shape every candidate signal must produce.

The gap this fills. The 6-year backtest over 13,403 signals found
`strategy_fit` has no edge at any horizon (hit rates 49.3-50.9%, every
|z| < 1.0). The response to that is to test other candidates — but there
was no seam to drop one into: `strategy_fit` returns `StrategyFit`, the
council returns something else, and `tests/eval/signal_backtest.py` reached
straight into `best_strategy`. Every new candidate meant new harness code,
which is how candidates end up untested.

So this is deliberately tiny. A model is a name and an `evaluate`. The
point is not abstraction for its own sake; it is that
`signal_backtest.py` can drive ANY implementation, so no candidate reaches
the live path without first clearing the same measurement that refuted the
incumbent.

**Explicitly NOT the investor personas** from `virattt/ai-hedge-fund`.
Those are candidate signals with no evidence behind any of them, and we
just proved we cannot tell a good candidate from a bad one by reading it.
Adding five unvalidated signals to a system with no edge produces a more
expensive coin flip. This is the interface for TESTING candidates, not an
invitation to add them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Signal:
    """One model's view on one symbol at one instant."""

    name: str
    """Which model produced this. Carried on the signal itself so a mixed
    population stays attributable after it is pooled for scoring."""

    symbol: str

    value: float
    """-1.0..+1.0. SIGN is the direction, MAGNITUDE is the conviction.

    One number rather than a direction plus a strength, because two fields
    allow a state that cannot mean anything — "short, conviction 0.9,
    direction flat" — and every consumer would then need the same guard.
    0.0 is flat."""

    abstained: bool = False
    """**The load-bearing field.** True means the model could not form a
    view: missing inputs, a failed call, a snapshot too thin to judge.

    It is NOT the same as `value == 0.0`, and collapsing them is the bug
    this flag exists to prevent. `value == 0.0` is "I looked and the
    evidence says nothing" — a real observation, and one a backtest should
    score. `abstained` is "I never formed a view", which must be EXCLUDED
    from scoring: counting silent failures as flat calls quietly drags any
    hit rate toward 50% and makes a broken model look merely mediocre.

    This mirrors the failure contract adopted for the LLM providers: a call
    or parse failure abstains; a DATA failure propagates and must never
    arrive here wearing this flag."""

    reason: str = ""
    """Named and greppable, for the audit row. Never model-written prose."""

    meta: dict[str, Any] = field(default_factory=dict)
    """Anything the model wants carried through to analysis — component
    scores, the chosen strategy id. Never read by the risk path."""

    def __post_init__(self) -> None:
        if self.abstained and self.value != 0.0:
            # An abstain with a non-zero value is a contradiction that would
            # otherwise be silently sized by a downstream consumer.
            object.__setattr__(self, "value", 0.0)
        if not -1.0 <= self.value <= 1.0:
            raise ValueError(f"Signal.value must be in [-1, 1], got {self.value!r}")

    @property
    def direction(self) -> str:
        """`long` / `short` / `flat`. Derived, never stored — see `value`."""
        if self.value > 0:
            return "long"
        return "short" if self.value < 0 else "flat"

    @classmethod
    def abstain(cls, name: str, symbol: str, reason: str) -> Signal:
        """The only way to build an abstention, so it always carries a
        reason. A silent abstain is indistinguishable from a flat call in
        the logs, which defeats the point of the flag."""
        return cls(name=name, symbol=symbol, value=0.0, abstained=True, reason=reason)


@runtime_checkable
class AlphaModel(Protocol):
    """A named thing that turns a feature snapshot into a `Signal`.

    `runtime_checkable` so the backtest can assert what it was handed
    rather than discovering the mismatch as an AttributeError mid-run.
    """

    @property
    def name(self) -> str: ...

    def evaluate(self, snapshot: dict[str, Any], *, symbol: str) -> Signal:
        """Judge `snapshot`. MUST NOT raise on thin or missing inputs —
        return `Signal.abstain(...)` instead. MUST NOT fetch anything: the
        snapshot is the whole input, which is what keeps a model replayable
        against historical bars (CLAUDE.md §3 — agents never originate raw
        data fetches)."""
        ...
