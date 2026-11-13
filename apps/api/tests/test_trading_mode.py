"""Admin runtime paper/live switch: resolver, routes, gate, auto-approver.

Resolver truth table (env LIVE_TRADING_ENABLED x stored admin value):

    env truthy                -> enabled,  source env
    env 0/false/no/off        -> disabled, source env, locked
    env typo (unrecognised)   -> disabled, locked (fail closed)
    env unset / empty         -> stored admin value (default off)
    store raises              -> disabled (fail closed)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")
os.environ.setdefault("DEMO_SESSION_ENABLED", "1")
os.environ.setdefault("DEMO_USER_ID", "77777777-7777-7777-7777-777777777777")

from app.core.config import get_settings
from app.main import app
from app.services.auth.auth_store import reset_auth_store_for_tests
from app.services.auth.jwt_service import mint_demo
from app.services.broker.broker_store import (
    BrokerConnectionRecord,
    reset_broker_store_for_tests,
)
from app.services.orders import auto_approver as aa
from app.services.orders.live_trading_gate import check_live_trading_gate
from app.services.platform import trading_mode as tm

ADMIN = "boss@example.com"
URL = "/api/v1/admin/trading-mode"
PHRASE = "ENABLE LIVE TRADING"


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    reset_auth_store_for_tests()
    reset_broker_store_for_tests()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login(c: TestClient, email: str) -> dict[str, str]:
    ch = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify", json={"email": email, "token": ch["devToken"]}
    ).json()
    return {"Authorization": f"Bearer {issued['accessToken']}"}


def _conn(*, is_paper: bool, consent: bool) -> BrokerConnectionRecord:
    return BrokerConnectionRecord(
        id="c1", user_id="u1", broker="alpaca", is_paper=is_paper, account_number=None,
        encrypted_access_token="x", encrypted_refresh_token=None,
        access_token_expires_at=None, refresh_token_expires_at=None,
        live_trading_consent=consent,
    )


# ── resolver ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
async def test_env_truthy_enables_even_if_admin_stored_off(monkeypatch, raw) -> None:
    await tm.set_live_trading(False, changed_by=ADMIN)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", raw)
    s = await tm.live_trading_state()
    assert (s.enabled, s.source, s.locked) == (True, "env", False)


@pytest.mark.parametrize("raw", ["0", "false", "No", "off", "maybe"])
async def test_env_falsy_or_typo_locks_off_even_if_admin_stored_on(monkeypatch, raw) -> None:
    await tm.set_live_trading(True, changed_by=ADMIN)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", raw)
    s = await tm.live_trading_state()
    assert (s.enabled, s.source, s.locked) == (False, "env", True)


@pytest.mark.parametrize("raw", [None, "", "  "])
async def test_env_unset_or_empty_defers_to_admin(monkeypatch, raw) -> None:
    if raw is not None:
        monkeypatch.setenv("LIVE_TRADING_ENABLED", raw)
    assert await tm.live_trading_state() == tm.LiveTradingState(False, "default", False)
    await tm.set_live_trading(True, changed_by=ADMIN)
    assert await tm.live_trading_state() == tm.LiveTradingState(True, "admin", False)
    await tm.set_live_trading(False, changed_by=ADMIN)
    assert await tm.live_trading_state() == tm.LiveTradingState(False, "admin", False)


async def test_store_error_fails_closed(monkeypatch) -> None:
    await tm.set_live_trading(True, changed_by=ADMIN)

    class Boom:
        async def get(self, key):
            raise RuntimeError("db down")

    monkeypatch.setattr(tm, "get_setting_store", lambda: Boom())
    s = await tm.live_trading_state()
    assert s.enabled is False


async def test_audit_row_written_per_change() -> None:
    await tm.set_live_trading(True, changed_by=ADMIN)
    await tm.set_live_trading(False, changed_by="other@example.com")
    rows = await tm.get_setting_store().audit(tm.SETTING_KEY)
    assert [(r.old_value, r.new_value, r.changed_by) for r in rows] == [
        (None, "true", ADMIN),
        ("true", "false", "other@example.com"),
    ]


# ── admin identity ───────────────────────────────────────────────────


def test_non_admin_post_is_403(client, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    h = _login(client, "someone@example.com")
    r = client.post(URL, json={"mode": "live", "confirm": PHRASE}, headers=h)
    assert r.status_code == 403
    assert client.get(URL, headers=h).json()["mode"] == "paper"


def test_admin_emails_unset_means_nobody(client) -> None:
    h = _login(client, ADMIN)
    assert client.post(URL, json={"mode": "live", "confirm": PHRASE}, headers=h).status_code == 403
    assert client.get(URL, headers=h).json()["isAdmin"] is False


def test_dev_bypass_never_admin(client, monkeypatch) -> None:
    from app.services.auth.auth_store import FIXTURE_USER_EMAIL

    monkeypatch.setenv("ADMIN_EMAILS", FIXTURE_USER_EMAIL)
    r = client.post(URL, json={"mode": "live", "confirm": PHRASE})  # no Bearer: dev bypass
    assert r.status_code == 401
    assert client.get(URL).status_code == 401


def test_demo_session_never_admin(client, monkeypatch) -> None:
    from app.services.auth.demo_session import DEMO_USER_EMAIL

    monkeypatch.setenv("ADMIN_EMAILS", DEMO_USER_EMAIL)
    tok = mint_demo(
        secret=get_settings().jwt_secret,
        user_id=os.environ["DEMO_USER_ID"],
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    access = client.post("/api/v1/auth/demo", json={"token": tok}).json()["accessToken"]
    h = {"Authorization": f"Bearer {access}"}
    assert client.post(URL, json={"mode": "live", "confirm": PHRASE}, headers=h).status_code == 401


# ── routes ───────────────────────────────────────────────────────────


def test_admin_email_match_is_case_insensitive_and_list(client, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", " a@x.com , BOSS@Example.com ")
    h = _login(client, ADMIN)
    body = client.get(URL, headers=h).json()
    assert body["isAdmin"] is True and body["mode"] == "paper" and body["source"] == "default"


@pytest.mark.parametrize("confirm", [None, "", "enable live trading", "ENABLE LIVE TRADING "])
def test_wrong_confirm_is_400_and_changes_nothing(client, monkeypatch, confirm) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    h = _login(client, ADMIN)
    r = client.post(URL, json={"mode": "live", "confirm": confirm}, headers=h)
    assert r.status_code == 400
    assert client.get(URL, headers=h).json()["mode"] == "paper"


def test_admin_switches_live_then_paper_without_phrase(client, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    h = _login(client, ADMIN)
    r = client.post(URL, json={"mode": "live", "confirm": PHRASE}, headers=h)
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["mode"], b["source"], b["locked"], b["updatedBy"]) == ("live", "admin", False, ADMIN)
    assert b["updatedAt"]
    r = client.post(URL, json={"mode": "paper"}, headers=h)
    assert r.status_code == 200 and r.json()["mode"] == "paper"


def test_locked_by_env_is_409(client, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "0")
    h = _login(client, ADMIN)
    r = client.post(URL, json={"mode": "live", "confirm": PHRASE}, headers=h)
    assert r.status_code == 409
    g = client.get(URL, headers=h).json()
    assert (g["mode"], g["source"], g["locked"]) == ("paper", "env", True)


def test_store_failure_on_switch_is_503_and_stays_paper(client, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    h = _login(client, ADMIN)

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(tm.get_setting_store(), "set", boom)
    r = client.post(URL, json={"mode": "live", "confirm": PHRASE}, headers=h)
    assert r.status_code == 503
    assert client.get(URL, headers=h).json()["mode"] == "paper"


# ── live gate ────────────────────────────────────────────────────────


async def test_gate_paper_connection_always_passes() -> None:
    assert await check_live_trading_gate(
        _conn(is_paper=True, consent=False), proposal_id="p", user_id="u1"
    ) is None


async def test_gate_refuses_live_connection_when_admin_switch_off() -> None:
    r = await check_live_trading_gate(
        _conn(is_paper=False, consent=True), proposal_id="p", user_id="u1"
    )
    assert r is not None and r.risk_veto_rule == "live_trading_disabled"
    assert "switched off by the admin" in (r.risk_reason or "")


async def test_gate_passes_when_admin_on_and_consent_but_not_without_consent() -> None:
    await tm.set_live_trading(True, changed_by=ADMIN)
    assert await check_live_trading_gate(
        _conn(is_paper=False, consent=True), proposal_id="p", user_id="u1"
    ) is None
    r = await check_live_trading_gate(
        _conn(is_paper=False, consent=False), proposal_id="p", user_id="u1"
    )
    assert r is not None and r.risk_veto_rule == "live_trading_disabled"
    assert "consent" in (r.risk_reason or "")


async def test_gate_env_lock_beats_admin_on(monkeypatch) -> None:
    await tm.set_live_trading(True, changed_by=ADMIN)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "0")
    r = await check_live_trading_gate(
        _conn(is_paper=False, consent=True), proposal_id="p", user_id="u1"
    )
    assert r is not None and "deploy-level lock" in (r.risk_reason or "")


# ── auto-approver ────────────────────────────────────────────────────


async def test_auto_approver_refuses_when_admin_switch_is_live(monkeypatch) -> None:
    monkeypatch.setenv("AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setenv("TRADING_MODE", "paper")
    reached: list[str] = []

    async def spy(user_id: str):
        reached.append(user_id)  # gate 2b: only reachable if gate 2 passed
        return None

    monkeypatch.setattr(aa, "_resolve_paper_connection", spy)

    # Control: switch off -> gate 2 passes and the sweep reaches gate 2b.
    assert await aa.auto_approve_for_user(user_id="u1", session_factory=object()) == 0
    assert reached == ["u1"]

    reached.clear()
    await tm.set_live_trading(True, changed_by=ADMIN)
    assert await aa.auto_approve_for_user(user_id="u1", session_factory=object()) == 0
    assert reached == [], "live on (admin) must stop the sweep at gate 2"
