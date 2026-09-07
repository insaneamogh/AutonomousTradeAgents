"""The resting broker stop must resolve its level the same way the ratchet
does — and must not blow up doing it.

This file exists because of a real miss: `sync_protective_stop` began
calling `effective_stop_loss_pct` while the import was absent, and the
whole suite still passed. Ruff's F821 caught it, not a test. That means
this branch had no coverage at all, and a NameError would have reached a
live exit path — the mechanism that is supposed to protect a position
overnight.
"""

from __future__ import annotations

import inspect

from engine.options.exits import effective_stop_loss_pct


def test_every_global_the_stop_path_references_actually_resolves() -> None:
    """The test that catches the real bug class.

    `sync_protective_stop` called `effective_stop_loss_pct` with no import.
    The whole suite still passed, because Python resolves globals at CALL
    time and nothing exercises this branch — so a NameError would have
    surfaced only when a real position needed its stop placed. Merely
    importing the module does not catch it either (that was this test's
    first, useless version).

    So: walk the function's own bytecode for the globals it references and
    assert each one resolves in the module namespace. Fast, needs no
    broker, and fails for exactly the reason we care about."""
    import builtins

    import app.services.orders.option_stops as mod

    checked = 0
    for fn in (mod.sync_protective_stop, mod._place):
        target = inspect.unwrap(fn)
        code = target.__code__
        for name in code.co_names:
            if name in vars(mod) or hasattr(builtins, name):
                checked += 1
                continue
            # co_names also holds attribute names (obj.attr), which are not
            # globals — only flag a name that is used as a bare call target
            # and is nowhere resolvable.
            if name in code.co_varnames or name in code.co_freevars:
                continue
            assert not _looks_like_a_bare_global(target, name), (
                f"{fn.__name__} references `{name}`, which resolves nowhere "
                "in its module — this is a NameError waiting for a live "
                "position to need its stop"
            )
    assert checked > 0


def _looks_like_a_bare_global(fn: object, name: str) -> bool:
    """True when `name` is loaded via LOAD_GLOBAL — i.e. genuinely expected
    to be a module-level name, not an attribute access."""
    import dis

    return any(
        i.opname == "LOAD_GLOBAL" and i.argval == name
        for i in dis.get_instructions(fn)  # type: ignore[arg-type]
    )


def test_the_resting_stop_resolves_the_per_position_level() -> None:
    """Both exit mechanisms must agree on the level. If the resting broker
    order and the local ladder disagreed, the same position would be
    protected at two different prices — and the broker's would win, since
    it sits at Alpaca and fires without us."""
    import app.services.orders.option_stops as mod

    src = inspect.getsource(mod.sync_protective_stop)
    assert "effective_stop_loss_pct" in src, (
        "sync_protective_stop must resolve the decision's own stop the same "
        "way position_manager._option_ratchet does"
    )


def test_resolver_agrees_across_both_call_sites() -> None:
    """Same inputs, same answer — the property that keeps the two exit
    mechanisms from drifting apart."""
    for decision_stop, cap, expected in [
        (35.0, 40.0, 35.0),   # agent tightens -> honoured
        (50.0, 40.0, 40.0),   # agent tries to loosen -> capped
        (None, 40.0, 40.0),   # absent -> cap
    ]:
        assert effective_stop_loss_pct(
            decision_stop_pct=decision_stop, cap_stop_pct=cap
        ) == expected
