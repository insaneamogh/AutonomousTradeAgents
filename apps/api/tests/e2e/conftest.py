"""E2E fixtures: one Postgres server per run, one fresh database per test.

Server, in order of preference:
  1. E2E_DATABASE_URL: an existing server you own (CI service container).
     The URL's database is only used to connect; tests create their own.
  2. A throwaway server started here with the local initdb/pg_ctl, on a
     free localhost port, deleted when the run ends.
  3. Neither available: every e2e scenario SKIPS, with that reason. The
     unit suite does not depend on them.

E2E_REUSE_TEMPLATE=1 skips re-migrating when the template already exists
(for repeated runs that only change application code).

Isolation (the OpenClaw pattern, applied to a database): the schema is
migrated ONCE into a template with the real Alembic migrations, and each
test gets `CREATE DATABASE ... TEMPLATE`, a full private copy in tens of
milliseconds. Nothing a scenario writes is visible to any other.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

REPO = Path(__file__).resolve().parents[4]
TEMPLATE_DB = "e2e_template"


def _pg_bindir() -> Path | None:
    """A directory holding initdb, pg_ctl AND the postgres server. Client
    packages (Homebrew's libpq) put initdb on PATH without a server."""
    candidates = []
    on_path = shutil.which("initdb")
    if on_path:
        candidates.append(Path(on_path).resolve().parent)
    for version in ("18", "17", "16", "15"):
        candidates += [Path(f"/opt/homebrew/opt/postgresql@{version}/bin"),
                       Path(f"/usr/local/opt/postgresql@{version}/bin"),
                       Path(f"/usr/lib/postgresql/{version}/bin")]
    for d in candidates:
        if all((d / n).exists() for n in ("initdb", "pg_ctl", "postgres")):
            return d
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _with_db(url: str, db: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


@pytest.fixture(scope="session")
def pg_server_url() -> Iterator[str]:
    """postgresql://... of a server we may create databases on."""
    external = os.environ.get("E2E_DATABASE_URL", "").strip()
    if external:
        yield external
        return
    bindir = _pg_bindir()
    if bindir is None:
        pytest.skip("e2e: no E2E_DATABASE_URL and no local Postgres server binaries")
    initdb, pg_ctl = str(bindir / "initdb"), str(bindir / "pg_ctl")
    data = Path(tempfile.mkdtemp(prefix="e2epg-"))
    port = _free_port()
    env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}
    subprocess.run([initdb, "-D", str(data / "db"), "-U", "postgres", "--auth=trust"],
                   check=True, capture_output=True, env=env)
    with open(data / "db" / "postgresql.conf", "a") as fh:
        fh.write(f"\nport = {port}\nlisten_addresses = '127.0.0.1'\n"
                 "unix_socket_directories = ''\nfsync = off\n"
                 "synchronous_commit = off\nfull_page_writes = off\n")
    subprocess.run([pg_ctl, "-D", str(data / "db"), "-l", str(data / "pg.log"), "-w", "start"],
                   check=True, capture_output=True, env=env)
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run([pg_ctl, "-D", str(data / "db"), "-m", "immediate", "stop"],
                       capture_output=True, env=env)
        shutil.rmtree(data, ignore_errors=True)


@pytest.fixture(scope="session")
def e2e_template(pg_server_url: str) -> str:
    """Create and migrate the template database once per run."""
    import asyncio

    import asyncpg

    reuse = os.environ.get("E2E_REUSE_TEMPLATE", "").strip() == "1"

    async def _create() -> bool:
        """True when a usable template already exists and may be reused.
        E2E_REUSE_TEMPLATE=1 is for repeated runs that change application
        code only (mutation analysis): migrations are not re-applied."""
        conn = await asyncpg.connect(pg_server_url)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", TEMPLATE_DB
            )
            if reuse and exists:
                return True
            await conn.execute(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{TEMPLATE_DB}"')
            return False
        finally:
            await conn.close()

    if asyncio.run(_create()):
        return TEMPLATE_DB
    env = {**os.environ, "DATABASE_URL": _with_db(pg_server_url, TEMPLATE_DB),
           "PYTHONDONTWRITEBYTECODE": "1"}
    alembic = str(Path(sys.executable).with_name("alembic"))
    proc = subprocess.run([alembic, "-c", "infra/migrations/alembic.ini", "upgrade", "head"],
                          cwd=REPO, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"e2e: migrations failed\n{proc.stderr[-2000:]}")
    return TEMPLATE_DB


@pytest.fixture
async def e2e_db(pg_server_url: str, e2e_template: str,
                 monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A private database for one test, wired into every session factory."""
    import asyncpg

    from engine.db import session as db_session

    name = f"e2e_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(pg_server_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{e2e_template}"')
    finally:
        await admin.close()

    monkeypatch.setenv("DATABASE_URL", _with_db(pg_server_url, name))
    monkeypatch.setenv("USE_POSTGRES", "1")
    monkeypatch.delenv("OPS_ALERT_WEBHOOK_URL", raising=False)
    _reset_singletons()
    try:
        yield name
    finally:
        await db_session.get_engine().dispose()
        _reset_singletons()
        admin = await asyncpg.connect(pg_server_url)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def _reset_singletons() -> None:
    """Every process-wide cache that could hold a session factory bound to
    another test's database."""
    from app.services.broker.broker_store import reset_broker_store_for_tests
    from app.services.council.store import reset_store_for_tests
    from app.services.notifications.ops_alerts import reset_ops_alerts_for_tests
    from engine.db import session as db_session

    db_session.get_engine.cache_clear()
    db_session._session_factory.cache_clear()
    reset_store_for_tests()
    reset_broker_store_for_tests()
    reset_ops_alerts_for_tests()


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every push the app tried to send, instead of sending it. Also keeps
    fire-and-forget notification tasks from outliving the test database."""
    from app.services.notifications import notifications

    sent: list[dict] = []

    def _record(**kw: object) -> None:
        sent.append(dict(kw))

    monkeypatch.setattr(notifications, "schedule_position_event_notification", _record)
    return sent
