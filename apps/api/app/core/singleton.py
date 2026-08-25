"""Lazy, env-switched singleton selection, in one place.

Six store modules each hand-rolled the same shape: a module-level singleton,
built on first call, choosing between a Postgres-backed implementation and
an in-memory/mock one based on ``env_flag("USE_POSTGRES")``. Harmless while
every copy agreed, but each one also had to remember to pair its singleton
with a matching reset-for-tests — a future copy that got the branching
slightly wrong, or dropped the reset half, would leak state across tests in
a way that's invisible until an unrelated test fails depending on run order.
"""

from __future__ import annotations

from typing import Callable, Generic, TypeVar

from engine.env import env_flag

__all__ = ["LazyEnvSingleton"]

T = TypeVar("T")


class LazyEnvSingleton(Generic[T]):
    """A process-wide singleton, picked once by an env flag, on first use.

    ``mock_factory`` builds the default implementation; ``postgres_factory``
    builds the one used when ``env_var`` (``USE_POSTGRES`` by default) is
    truthy. Both are zero-arg callables rather than bare classes: a caller
    whose Postgres impl needs a lazy import — deferring an optional/heavy
    module until it's actually selected — wraps that import in a small
    function, while a caller whose impl is already in scope can just pass
    the class itself (a no-arg constructor already satisfies
    ``Callable[[], T]``).
    """

    def __init__(
        self,
        mock_factory: Callable[[], T],
        postgres_factory: Callable[[], T],
        *,
        env_var: str = "USE_POSTGRES",
    ) -> None:
        self._mock_factory = mock_factory
        self._postgres_factory = postgres_factory
        self._env_var = env_var
        self._instance: T | None = None

    def get(self) -> T:
        """Return the singleton, building it via the right factory on first call."""
        if self._instance is None:
            factory = self._postgres_factory if env_flag(self._env_var) else self._mock_factory
            self._instance = factory()
        return self._instance

    def reset(self) -> None:
        """Drop the singleton. Tests call this to force a clean rebuild."""
        self._instance = None
