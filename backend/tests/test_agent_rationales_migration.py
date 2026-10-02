"""SCRUM-53 Phase B / SCRUM-67: runs the real migration chain (`alembic
upgrade head` / `downgrade -1`) against a throwaway Postgres database (the
migrated_scratch_database_url fixture in tests/conftest.py) -- never the
shared session schema, since the downgrade changes it.

SCRUM-67: this used to run against an on-disk SQLite file, where the DDL
applied but every migrated table was unusable -- each migration's
`server_default=sa.text('now()')` is Postgres-only, so any insert relying on
it failed with "unknown function: now()". The insert test below is the
check SQLite could never pass.
"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from alembic.config import Config
from sqlalchemy import MetaData, create_engine, insert, inspect

from alembic import command
from app.database import Base

BACKEND_DIR = Path(__file__).parent.parent

# One row per migrated table with a created_at column, in FK order, leaving
# created_at out so the migration's server default has to supply it.
ROWS_WITHOUT_CREATED_AT = {
    "users": {"name": "Migration Check User"},
    "transactions": {
        "user_id": 1,
        "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "merchant": "Migration Check Merchant",
        "category": "groceries",
        "amount": Decimal("10.00"),
        "latitude": 47.6062,
        "longitude": -122.3321,
        "location_label": "Seattle, WA",
    },
    "transaction_flags": {"transaction_id": 1, "rule_name": "velocity", "rationale": "Flagged: velocity reason."},
    "agent_rationales": {
        "transaction_id": 1,
        "fact_fingerprint": "fingerprint",
        "prompt_version": "v1",
        "model_id": "mock",
        "validation_passed": True,
        "violations": [],
    },
}


def test_alembic_upgrade_head_applies_cleanly(migrated_scratch_database_url):
    engine = create_engine(migrated_scratch_database_url)
    inspector = inspect(engine)

    assert "agent_rationales" in inspector.get_table_names()

    columns = {col["name"] for col in inspector.get_columns("agent_rationales")}
    assert columns == {
        "id",
        "transaction_id",
        "fact_fingerprint",
        "prompt_version",
        "model_id",
        "rationale",
        "validation_passed",
        "violations",
        "composition_error",
        "composed_text",
        "validator_version",
        "created_at",
    }

    index_names = {idx["name"] for idx in inspector.get_indexes("agent_rationales")}
    assert "ix_agent_rationales_transaction_fingerprint_prompt_version" in index_names
    engine.dispose()


def test_server_default_created_at_works_on_every_migrated_table(migrated_scratch_database_url):
    tables_with_created_at = {table.name for table in Base.metadata.sorted_tables if "created_at" in table.c}
    # A new table with a created_at column needs a row above, or this test
    # silently stops covering it.
    assert set(ROWS_WITHOUT_CREATED_AT) == tables_with_created_at

    engine = create_engine(migrated_scratch_database_url)
    # Reflected from the migrated database, not Base.metadata, so the insert
    # exercises the defaults the migrations actually created.
    migrated = MetaData()
    migrated.reflect(bind=engine)
    with engine.begin() as conn:
        for table_name, row in ROWS_WITHOUT_CREATED_AT.items():
            table = migrated.tables[table_name]
            created_at = conn.execute(insert(table).values(**row).returning(table.c.created_at)).scalar_one()
            assert created_at is not None, table_name
            assert created_at.tzinfo is not None, table_name
    engine.dispose()


def test_alembic_downgrade_one_step_removes_just_the_follow_up_columns(migrated_scratch_database_url):
    """SCRUM-53 follow-up migration (composed_text, validator_version):
    downgrading by one step must remove exactly those two columns and
    nothing else -- the table and its original columns/index stay, matching
    upgrade -1's own down_revision target."""
    engine = create_engine(migrated_scratch_database_url)
    inspector = inspect(engine)
    assert {"composed_text", "validator_version"} <= {
        col["name"] for col in inspector.get_columns("agent_rationales")
    }
    engine.dispose()

    # migrated_scratch_database_url leaves DATABASE_URL pointed at the
    # scratch database, which is what alembic/env.py reads.
    command.downgrade(Config(str(BACKEND_DIR / "alembic.ini")), "-1")

    engine = create_engine(migrated_scratch_database_url)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("agent_rationales")}
    assert "composed_text" not in columns
    assert "validator_version" not in columns
    assert "agent_rationales" in inspector.get_table_names()  # table itself untouched
    index_names = {idx["name"] for idx in inspector.get_indexes("agent_rationales")}
    assert "ix_agent_rationales_transaction_fingerprint_prompt_version" in index_names
    engine.dispose()
