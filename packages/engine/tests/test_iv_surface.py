"""Constant-maturity ATM IV and IV rank: the arithmetic behind iv_history."""

from __future__ import annotations

import importlib.util
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine.options.iv_surface import (
    IV_RANK_MIN_OBS,
    IvPoint,
    atm_iv_by_expiry,
    constant_maturity_iv,
    iv_rank,
)

TODAY = date(2026, 9, 23)


@dataclass(frozen=True)
class Q:
    contract_type: str
    expiry: date
    delta: float | None
    implied_volatility: float | None


def _exp(days: int) -> date:
    return TODAY + timedelta(days=days)


def test_atm_is_the_call_and_put_nearest_half_delta_averaged() -> None:
    chain = [
        Q("call", _exp(30), 0.52, 0.30), Q("call", _exp(30), 0.20, 0.45),
        Q("put", _exp(30), -0.48, 0.34), Q("put", _exp(30), -0.10, 0.60),
    ]
    [p] = atm_iv_by_expiry(chain, TODAY)
    assert p.dte == 30 and p.atm_iv == pytest.approx(0.32)


def test_a_wings_only_expiry_is_not_passed_off_as_atm() -> None:
    chain = [Q("call", _exp(30), 0.15, 0.55), Q("put", _exp(30), -0.12, 0.62)]
    assert atm_iv_by_expiry(chain, TODAY) == []


def test_expiries_inside_a_week_are_excluded() -> None:
    assert atm_iv_by_expiry([Q("call", _exp(3), 0.5, 0.9)], TODAY) == []


def test_constant_maturity_interpolates_total_variance() -> None:
    pts = [IvPoint(20, 0.30), IvPoint(40, 0.40)]
    var = 0.30**2 * 20 + 0.5 * (0.40**2 * 40 - 0.30**2 * 20)
    assert constant_maturity_iv(pts, 30) == pytest.approx(math.sqrt(var / 30))


def test_constant_maturity_refuses_to_extrapolate_far() -> None:
    assert constant_maturity_iv([IvPoint(35, 0.3)], 30) == 0.3      # 5 days: ok
    assert constant_maturity_iv([IvPoint(45, 0.3)], 30) is None     # 15 days: no
    assert constant_maturity_iv([], 30) is None


def test_iv_rank_needs_enough_history_then_ranks() -> None:
    short = [0.2 + 0.001 * i for i in range(IV_RANK_MIN_OBS - 1)]
    assert iv_rank(short, 0.25) is None
    hist = [0.20] * 30 + [0.40] * 40
    assert iv_rank(hist, 0.30) == 50.0
    assert iv_rank(hist, 0.40) == 100.0


def test_the_migration_creates_exactly_the_model_columns() -> None:
    """Postgres-backed tests are gated behind a live DB, so this checks the
    one thing that would otherwise surface only at deploy: migration 0019
    and the IvHistory model agree on the columns."""
    from engine.db.models import IvHistory

    root = Path(__file__).resolve().parents[3]
    path = root / "infra" / "migrations" / "versions" / "20260923_0019_iv_history.py"
    spec = importlib.util.spec_from_file_location("m0019", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)

    created: dict[str, list[str]] = {}

    class _Op:
        def create_table(self, name, *cols, **_):
            created[name] = [c.name for c in cols]

    import alembic

    real = alembic.op
    try:
        alembic.op = _Op()  # type: ignore[assignment]
        spec.loader.exec_module(mod)
        mod.op = alembic.op
        mod.upgrade()
    finally:
        alembic.op = real  # type: ignore[assignment]
    assert sorted(created["iv_history"]) == sorted(IvHistory.__table__.c.keys())
