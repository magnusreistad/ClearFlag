"""SCRUM-53 Phase B: scripts.compose_rationales -- mock mode only, no
network. Own in-memory SQLite engine, monkeypatched onto the script's own
SessionLocal so it never touches the real Neon DB.
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.investigation_agent import graph as graph_module
from app.investigation_agent.validation import VALIDATOR_VERSION
from app.models import AgentRationale, Transaction, User
from scripts import compose_rationales

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

GROUNDED_RATIONALE = (
    "This $500.00 purchase is far above your typical spend in this category, "
    "standing out clearly from your usual pattern."
)
UNGROUNDED_RATIONALE = "This looks like it happened in Wakanda, which is unusual."


@pytest.fixture(autouse=True)
def db_schema(monkeypatch):
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(compose_rationales, "SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=engine)


def _fake_model_sequence(texts: list[str]):
    """get_chat_model() is called once per composition attempt -- this
    returns a factory that hands back a fresh single-message fake model per
    call, consuming `texts` in order, so each attempt in a multi-transaction
    test gets its own scripted response."""
    texts_iter = iter(texts)
    return lambda: GenericFakeChatModel(messages=iter([AIMessage(content=next(texts_iter))]))


def _add_amount_deviation_outlier(db, user_id: int, *, category: str = "groceries", base_time=None) -> int:
    base_time = base_time or datetime.now(timezone.utc)
    history = [
        Transaction(
            user_id=user_id,
            timestamp=base_time - timedelta(days=10 - i),
            merchant="Test Merchant",
            category=category,
            amount=Decimal("10.00"),
            latitude=47.6062,
            longitude=-122.3321,
            location_label="Seattle, WA",
        )
        for i in range(5)
    ]
    outlier = Transaction(
        user_id=user_id,
        timestamp=base_time,
        merchant="Test Merchant",
        category=category,
        amount=Decimal("500.00"),
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )
    db.add_all([*history, outlier])
    db.flush()
    return outlier.id


def _seed_flagged_user() -> tuple[int, int]:
    db = TestingSessionLocal()
    user = User(name="Compose Script User")
    db.add(user)
    db.flush()
    outlier_id = _add_amount_deviation_outlier(db, user.id)
    db.commit()
    user_id = user.id
    db.close()
    return user_id, outlier_id


class TestComposeAndSkip:
    def test_composes_a_pending_transaction_and_records_a_pass(self, monkeypatch, capsys):
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([GROUNDED_RATIONALE]))
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        compose_rationales.main()

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()

        assert len(rows) == 1
        assert rows[0].validation_passed is True
        assert rows[0].rationale == GROUNDED_RATIONALE
        # SCRUM-53 follow-up: on a pass, composed_text duplicates rationale
        # (both answer the same question here), and validator_version is
        # recorded on every new row.
        assert rows[0].composed_text == GROUNDED_RATIONALE
        assert rows[0].validator_version == VALIDATOR_VERSION

        out = capsys.readouterr().out
        assert "composed=1 passed=1 failed=0 skipped=0" in out

    def test_second_run_skips_the_already_passing_transaction(self, monkeypatch, capsys):
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([GROUNDED_RATIONALE]))
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])
        compose_rationales.main()
        capsys.readouterr()  # discard first run's output

        # No further scripted responses at all: if the script tried to
        # compose again, get_chat_model would raise StopIteration.
        compose_rationales.main()

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()
        assert len(rows) == 1  # not duplicated

        out = capsys.readouterr().out
        assert "composed=0 passed=0 failed=0 skipped=1" in out

    def test_records_a_failed_composition_without_crashing(self, monkeypatch, capsys):
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([UNGROUNDED_RATIONALE]))
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        compose_rationales.main()

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()

        assert len(rows) == 1
        assert rows[0].validation_passed is False
        assert rows[0].rationale is None
        assert rows[0].violations  # the ungrounded-entity violation was recorded
        # SCRUM-53 follow-up: composed_text (unlike rationale) is populated
        # on a failure too -- the audit record of what the model actually
        # produced -- and validator_version records which validator judged it.
        assert rows[0].composed_text == UNGROUNDED_RATIONALE
        assert rows[0].validator_version == VALIDATOR_VERSION

        out = capsys.readouterr().out
        assert "composed=1 passed=0 failed=1 skipped=0" in out


class TestDryRun:
    def test_dry_run_makes_no_model_calls_and_no_writes(self, monkeypatch, capsys):
        def _raise():
            raise AssertionError("dry run must never construct a chat model")

        monkeypatch.setattr(graph_module, "get_chat_model", _raise)
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id), "--dry-run"])

        compose_rationales.main()

        db = TestingSessionLocal()
        assert db.query(AgentRationale).count() == 0
        db.close()

        out = capsys.readouterr().out
        assert f"transaction {outlier_id}" in out
        assert "composed=0 passed=0 failed=0 skipped=0" in out


class TestLimit:
    def test_limit_bounds_how_many_are_composed_in_one_run(self, monkeypatch, capsys):
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([GROUNDED_RATIONALE, GROUNDED_RATIONALE]))

        db = TestingSessionLocal()
        user = User(name="Limit User")
        db.add(user)
        db.flush()
        base_time = datetime.now(timezone.utc)
        # Two independent amount-deviation outliers in two different
        # categories, so this user has two separate flagged transactions.
        _add_amount_deviation_outlier(db, user.id, category="groceries", base_time=base_time)
        _add_amount_deviation_outlier(db, user.id, category="dining", base_time=base_time)
        db.commit()
        user_id = user.id
        db.close()

        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id), "--limit", "1"])
        compose_rationales.main()

        db = TestingSessionLocal()
        composed_count = db.query(AgentRationale).count()
        db.close()
        assert composed_count == 1

        out = capsys.readouterr().out
        assert "composed=1 passed=1 failed=0 skipped=0" in out
