"""Shared Postgres test harness (SCRUM-67).

Every test runs against a real Postgres (TEST_DATABASE_URL -- see README's
"Running backend tests"), not SQLite: SQLite dropped timezone offsets on
round-trip, couldn't run the migrations' `now()` server defaults, and hid
connection-level behavior (locking, aborted transactions) behind a single
shared StaticPool connection.

One engine, one way in: DATABASE_URL is pointed at TEST_DATABASE_URL here,
before anything imports app.database, so the app's OWN engine, SessionLocal
and get_db are the test ones. Nothing overrides get_db or monkeypatches a
module's SessionLocal -- a code path that opens its own session (the
Investigation Agent's tools, scripts.compose_rationales) reaches the test
database without the test having to know it exists. It also means a test
can never fall through to the real Neon DATABASE_URL in backend/.env
(load_dotenv() never overrides a variable that's already set).

Schema: built once per session by `alembic upgrade head` -- the same path
production takes -- not Base.metadata.create_all(); test_schema_drift.py
checks the two agree. Isolation: every table is TRUNCATEd at the START of
each test (so a test that crashed mid-way can't leak rows into the next),
under a short lock_timeout so a session a previous test leaked fails loudly
instead of hanging the run.

Tests that change the schema itself use `scratch_database_url` /
`migrated_scratch_database_url`: a throwaway database created and dropped
around that one test, so they can never break the shared schema.

No live calls, ever (SCRUM-54, see tests/no_live_guard.py): the environment
is checked right after load_dotenv(), before anything imports app.*, and an
autouse session fixture blocks all non-loopback network access.
"""

import os
import uuid

import pytest
from dotenv import load_dotenv
from no_live_guard import enforce_environment, install_network_block

# Lets TEST_DATABASE_URL live in backend/.env alongside DATABASE_URL; a value
# already in the environment (e.g. CI's) still wins.
load_dotenv()
enforce_environment()
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise pytest.UsageError(
        "TEST_DATABASE_URL is not set. The backend tests run against Postgres -- "
        "see README.md, 'Running backend tests', for the one-command local setup."
    )

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Every test TRUNCATEs every table, so refuse anything that doesn't look like
# a dedicated test database -- e.g. the Neon dev branch pasted in by mistake.
if "test" not in (make_url(TEST_DATABASE_URL).database or ""):
    raise pytest.UsageError(
        f"TEST_DATABASE_URL's database name must contain 'test' (got "
        f"{make_url(TEST_DATABASE_URL).database!r}); every test truncates every table."
    )

os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from alembic.config import Config

from alembic import command
from app import models  # noqa: F401  (registers every table on Base.metadata)
from app.database import Base, engine

ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
TRUNCATE_LOCK_TIMEOUT = "5s"


def alembic_config() -> Config:
    """alembic/env.py reads DATABASE_URL from the environment, so point that
    (e.g. via monkeypatch.setenv) at the target database before running a
    command with this config."""
    return Config(ALEMBIC_INI)


@pytest.fixture(scope="session", autouse=True)
def block_outbound_network():
    """Defined first so it's in place before any other session fixture runs."""
    with pytest.MonkeyPatch.context() as mp:
        install_network_block(mp)
        yield


@pytest.fixture(scope="session", autouse=True)
def migrated_schema(block_outbound_network):
    """Rebuilds the shared test database from nothing via the migrations, so
    every run starts from exactly what `alembic upgrade head` produces."""
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(), "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(migrated_schema):
    table_names = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{TRUNCATE_LOCK_TIMEOUT}'"))
        conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def scripted_llm(monkeypatch):
    """SCRUM-54: scripted_llm(*responses) installs a tests/agent_fixtures.py
    ScriptedLLM and returns it; may be called again mid-test to re-script.
    Fails the test if any model call went unscripted."""
    from agent_fixtures import ScriptedLLM

    installed = []

    def install(*responses):
        scripted = ScriptedLLM(responses).install(monkeypatch)
        installed.append(scripted)
        return scripted

    yield install
    assert sum(s.unscripted_calls for s in installed) == 0, "a model call was made that the test didn't script"


@pytest.fixture
def scratch_database_url():
    """An empty, throwaway database on the same server as TEST_DATABASE_URL,
    dropped (connections and all) after the test."""
    name = f"clearflag_test_scratch_{uuid.uuid4().hex[:12]}"
    url = make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def migrated_scratch_database_url(scratch_database_url, monkeypatch):
    """scratch_database_url, upgraded to head. Leaves DATABASE_URL pointed at
    the scratch database for the rest of the test, so further alembic
    commands (e.g. a downgrade) target it too. The app's own engine was
    already created against TEST_DATABASE_URL and is unaffected."""
    monkeypatch.setenv("DATABASE_URL", scratch_database_url)
    command.upgrade(alembic_config(), "head")
    return scratch_database_url
