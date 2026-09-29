"""SCRUM-53 Phase B: the agent_rationales migration must work on both
Postgres (applied to the Neon dev branch) and the SQLite test harness. This
actually runs `alembic upgrade head` against a throwaway on-disk SQLite file
-- not just app.models.AgentRationale via Base.metadata.create_all -- so a
SQLite-incompatible statement in the generated migration itself (not just
the ORM model) would be caught here.
"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command

BACKEND_DIR = Path(__file__).parent.parent


def test_alembic_upgrade_head_applies_cleanly_on_sqlite(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_check.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
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


def test_alembic_downgrade_one_step_removes_just_the_follow_up_columns(tmp_path, monkeypatch):
    """SCRUM-53 follow-up migration (composed_text, validator_version):
    downgrading by one step must remove exactly those two columns and
    nothing else -- the table and its original columns/index stay, matching
    upgrade -1's own down_revision target."""
    db_path = tmp_path / "downgrade_check.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert {"composed_text", "validator_version"} <= {
        col["name"] for col in inspector.get_columns("agent_rationales")
    }
    engine.dispose()

    command.downgrade(config, "-1")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("agent_rationales")}
    assert "composed_text" not in columns
    assert "validator_version" not in columns
    assert "agent_rationales" in inspector.get_table_names()  # table itself untouched
    index_names = {idx["name"] for idx in inspector.get_indexes("agent_rationales")}
    assert "ix_agent_rationales_transaction_fingerprint_prompt_version" in index_names
