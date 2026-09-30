"""SCRUM-53 Phase B / SCRUM-56: scripts.compose_rationales -- mock mode only,
no network. Own in-memory SQLite engine, monkeypatched onto the script's own
SessionLocal so it never touches the real Neon DB.

SCRUM-56 switched the script's logging from print() to a module logger
(logging.getLogger(__name__)) -- assertions on its output use pytest's
caplog fixture rather than capsys from here on, except TestDryRun, which
still uses print() (see scripts.compose_rationales.main's --dry-run branch)
and so still uses capsys. Every caplog.set_level call names
logger="scripts.compose_rationales" explicitly (rather than the root
logger) -- alembic.ini sets the ROOT logger's level to WARNING, and
Alembic's env.py runs inside the same process for
tests/test_agent_rationales_migration.py, so a root-level override would be
one more test-ordering trap; naming this module's own logger sets its
level directly, independent of whatever the root logger's level happens to
be at the time.
"""

import logging
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
from app.investigation_agent.fingerprint import compute_fact_fingerprint
from app.investigation_agent.payload import build_payload
from app.investigation_agent.prompts import PROMPT_VERSION
from app.investigation_agent.state import transaction_data_from_orm
from app.investigation_agent.validation import (
    MAX_VALIDATION_ATTEMPTS,
    VALIDATOR_VERSION,
)
from app.models import AgentRationale, Transaction, User
from app.rules.engine import (
    evaluate_all_rules,
    rule_names_by_transaction,
    rule_values_by_transaction,
)
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
    def test_composes_a_pending_transaction_and_records_a_pass(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
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

        assert "run complete: passed=1 validation_failed=0 composition_error=0 skipped_capped=0 skipped_already_passing=0" in caplog.text
        assert f"transaction={outlier_id}" in caplog.text
        assert "outcome=passed" in caplog.text
        assert f"attempt=1/{MAX_VALIDATION_ATTEMPTS}" in caplog.text

    def test_second_run_skips_the_already_passing_transaction(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([GROUNDED_RATIONALE]))
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])
        compose_rationales.main()
        caplog.clear()  # discard first run's output

        # No further scripted responses at all: if the script tried to
        # compose again, get_chat_model would raise StopIteration.
        compose_rationales.main()

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()
        assert len(rows) == 1  # not duplicated

        assert "run complete: passed=0 validation_failed=0 composition_error=0 skipped_capped=0 skipped_already_passing=1" in caplog.text
        assert f"transaction={outlier_id}" in caplog.text
        assert "outcome=skipped_already_passing" in caplog.text

    def test_records_a_failed_composition_without_crashing(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
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

        assert "run complete: passed=0 validation_failed=1 composition_error=0 skipped_capped=0 skipped_already_passing=0" in caplog.text
        assert "outcome=validation_failed" in caplog.text
        assert f"attempt=1/{MAX_VALIDATION_ATTEMPTS}" in caplog.text
        # Violation types are logged (SCRUM-56); the composed text/prompt/violation
        # spans that produced them must never appear anywhere in the log output.
        assert "violation_types=" in caplog.text
        assert UNGROUNDED_RATIONALE not in caplog.text


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
    def test_limit_bounds_how_many_are_composed_in_one_run(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
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

        assert "run complete: passed=1 validation_failed=0 composition_error=0 skipped_capped=0 skipped_already_passing=0" in caplog.text


class _CountingModel:
    """Asserts a model was (or wasn't) actually invoked, without relying on
    an exception propagating -- compose_rationale (app.investigation_agent.
    graph) always catches a model-call exception itself and records it as a
    composition_error, so a raising fake would never surface as a loud test
    failure the way it does elsewhere in this file (e.g. the already-passing
    test's StopIteration trick)."""

    def __init__(self, response_text: str = UNGROUNDED_RATIONALE):
        self.calls = 0
        self._response_text = response_text

    def invoke(self, *_args, **_kwargs):
        self.calls += 1
        return AIMessage(content=self._response_text)


class TestRetryCap:
    """SCRUM-56: MAX_VALIDATION_ATTEMPTS validation-failed attempts for a
    transaction's (transaction_id, fact_fingerprint, prompt_version,
    VALIDATOR_VERSION) budget key caps further composition -- but only
    validation failures consume that budget; composition_error attempts,
    and rows from a different budget key (a validator_version bump, or a
    row with validator_version IS NULL), don't count."""

    def test_cap_skips_the_third_attempt_without_calling_the_model(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([UNGROUNDED_RATIONALE]))
        compose_rationales.main()
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([UNGROUNDED_RATIONALE]))
        compose_rationales.main()
        caplog.clear()

        counting_model = _CountingModel()
        monkeypatch.setattr(graph_module, "get_chat_model", lambda: counting_model)
        compose_rationales.main()

        assert counting_model.calls == 0  # the third run never even tries

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()
        assert len(rows) == 2  # capped after the second validation-failed attempt, not a third

        assert "outcome=skipped_capped" in caplog.text
        assert f"attempt={MAX_VALIDATION_ATTEMPTS}/{MAX_VALIDATION_ATTEMPTS}" in caplog.text
        assert (
            "run complete: passed=0 validation_failed=0 composition_error=0 "
            "skipped_capped=1 skipped_already_passing=0"
        ) in caplog.text

    def test_composition_error_attempts_do_not_consume_the_budget(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        class _RaisingModel:
            def invoke(self, *_args, **_kwargs):
                raise TimeoutError("simulated model timeout")

        # Three runs, each a model failure -- if composition_error consumed
        # the same budget as a validation failure, the third would be capped
        # (MAX_VALIDATION_ATTEMPTS == 2) and never attempted at all. Each run
        # exits non-zero (SCRUM-56: any composition_error in a run does) --
        # that's exercised for its own sake in TestExitCode below, so it's
        # just caught and ignored here.
        for _ in range(3):
            monkeypatch.setattr(graph_module, "get_chat_model", lambda: _RaisingModel())
            with pytest.raises(SystemExit) as exc_info:
                compose_rationales.main()
            assert exc_info.value.code == 1

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()
        assert len(rows) == 3
        assert all(row.composition_error is not None for row in rows)

        assert "outcome=skipped_capped" not in caplog.text

    def test_validator_version_bump_resets_the_budget(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([UNGROUNDED_RATIONALE]))
        compose_rationales.main()
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([UNGROUNDED_RATIONALE]))
        compose_rationales.main()
        caplog.clear()

        # Confirm it really is capped under the current validator version first.
        counting_model = _CountingModel()
        monkeypatch.setattr(graph_module, "get_chat_model", lambda: counting_model)
        compose_rationales.main()
        assert counting_model.calls == 0
        caplog.clear()

        # A VALIDATOR_VERSION bump changes the budget key -- the two capped
        # attempts above no longer match it, so the budget is fresh again.
        monkeypatch.setattr(compose_rationales, "VALIDATOR_VERSION", "v99-test")
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([GROUNDED_RATIONALE]))
        compose_rationales.main()

        db = TestingSessionLocal()
        rows = (
            db.query(AgentRationale)
            .filter(AgentRationale.transaction_id == outlier_id)
            .order_by(AgentRationale.id)
            .all()
        )
        db.close()
        assert len(rows) == 3
        assert rows[-1].validator_version == "v99-test"
        assert rows[-1].validation_passed is True
        assert "outcome=passed" in caplog.text

    def test_null_validator_version_row_does_not_consume_budget(self, monkeypatch, caplog):
        """A row written before the validator_version column existed
        (validator_version IS NULL) never matches VALIDATOR_VERSION by
        exact-equality, so it doesn't count toward the cap -- confirmed here
        by inserting one directly (bypassing the script, since the script
        itself always writes the current VALIDATOR_VERSION) and checking the
        very next run still attempts composition rather than skipping it."""
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        user_id, outlier_id = _seed_flagged_user()

        db = TestingSessionLocal()
        transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
        hits = evaluate_all_rules(transactions)
        rule_names_by_id = rule_names_by_transaction(hits)
        rule_values_by_id = rule_values_by_transaction(hits)
        outlier = next(t for t in transactions if t.id == outlier_id)
        fingerprint = compute_fact_fingerprint(
            build_payload(
                transaction_data_from_orm(outlier), rule_names_by_id[outlier_id], rule_values_by_id.get(outlier_id, {})
            )
        )
        db.add(
            AgentRationale(
                transaction_id=outlier_id,
                fact_fingerprint=fingerprint,
                prompt_version=PROMPT_VERSION,
                model_id="legacy",
                rationale=None,
                validation_passed=False,
                violations=[],
                composition_error=None,
                composed_text="a legacy failed attempt",
                validator_version=None,
            )
        )
        db.commit()
        db.close()

        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([GROUNDED_RATIONALE]))

        compose_rationales.main()

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()
        assert len(rows) == 2  # the legacy row, plus this run's new attempt
        assert "outcome=skipped_capped" not in caplog.text
        assert "outcome=passed" in caplog.text

    def test_capped_transactions_dont_count_against_limit(self, monkeypatch, caplog):
        """Capping filters happen in _collect_pending, BEFORE --limit slices
        the pending list -- a capped transaction must not use up a run slot
        that a transaction still worth attempting could have had."""
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")

        db = TestingSessionLocal()
        user = User(name="Cap Limit User")
        db.add(user)
        db.flush()
        base_time = datetime.now(timezone.utc)
        capped_id = _add_amount_deviation_outlier(db, user.id, category="groceries", base_time=base_time)
        db.commit()
        user_id = user.id
        db.close()

        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([UNGROUNDED_RATIONALE]))
        compose_rationales.main()
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([UNGROUNDED_RATIONALE]))
        compose_rationales.main()
        caplog.clear()

        # A second, fresh outlier for the same user, added after the first is capped.
        db = TestingSessionLocal()
        fresh_id = _add_amount_deviation_outlier(db, user_id, category="dining", base_time=base_time)
        db.commit()
        db.close()

        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id), "--limit", "1"])
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([GROUNDED_RATIONALE]))
        compose_rationales.main()

        db = TestingSessionLocal()
        fresh_rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == fresh_id).all()
        capped_rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == capped_id).all()
        db.close()

        assert len(fresh_rows) == 1
        assert fresh_rows[0].validation_passed is True
        assert len(capped_rows) == 2  # unchanged -- the capped transaction was never attempted a third time

        assert f"transaction={fresh_id}" in caplog.text
        assert "outcome=passed" in caplog.text
        assert f"transaction={capped_id}" in caplog.text
        assert "outcome=skipped_capped" in caplog.text


class TestOneFailureDoesntStopTheRest:
    def test_one_transactions_model_error_doesnt_stop_the_others_in_the_run(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")

        db = TestingSessionLocal()
        user = User(name="Mixed Outcome User")
        db.add(user)
        db.flush()
        base_time = datetime.now(timezone.utc)
        failing_id = _add_amount_deviation_outlier(db, user.id, category="groceries", base_time=base_time)
        passing_id = _add_amount_deviation_outlier(db, user.id, category="dining", base_time=base_time)
        db.commit()
        user_id = user.id
        db.close()

        # _collect_pending returns oldest-transaction-id-first, so the
        # groceries outlier (added first, lower id) is composed before the
        # dining one -- scripting the model to fail on the first call and
        # succeed on the second exercises exactly that ordering.
        responses = iter([TimeoutError("simulated model timeout"), GROUNDED_RATIONALE])

        class _SequencedModel:
            def invoke(self, *_args, **_kwargs):
                response = next(responses)
                if isinstance(response, Exception):
                    raise response
                return AIMessage(content=response)

        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _SequencedModel())
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        with pytest.raises(SystemExit) as exc_info:
            compose_rationales.main()
        assert exc_info.value.code == 1

        db = TestingSessionLocal()
        failing_rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == failing_id).all()
        passing_rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == passing_id).all()
        db.close()

        assert len(failing_rows) == 1
        assert failing_rows[0].composition_error is not None
        assert len(passing_rows) == 1  # still attempted, and passed, despite the other transaction's failure
        assert passing_rows[0].validation_passed is True

        assert "run complete: passed=1 validation_failed=0 composition_error=1 skipped_capped=0 skipped_already_passing=0" in caplog.text


class TestExitCode:
    def test_exits_zero_when_every_attempt_passes(self, monkeypatch):
        user_id, _outlier_id = _seed_flagged_user()
        monkeypatch.setattr(graph_module, "get_chat_model", _fake_model_sequence([GROUNDED_RATIONALE]))
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        compose_rationales.main()  # would raise SystemExit if it tried to exit non-zero

    def test_exits_non_zero_when_a_composition_error_occurs(self, monkeypatch):
        user_id, _outlier_id = _seed_flagged_user()

        class _RaisingModel:
            def invoke(self, *_args, **_kwargs):
                raise TimeoutError("simulated model timeout")

        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _RaisingModel())
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        with pytest.raises(SystemExit) as exc_info:
            compose_rationales.main()
        assert exc_info.value.code == 1

    def test_dry_run_always_exits_zero_even_with_pending_work(self, monkeypatch):
        user_id, _outlier_id = _seed_flagged_user()

        def _raise():
            raise AssertionError("dry run must never construct a chat model")

        monkeypatch.setattr(graph_module, "get_chat_model", _raise)
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id), "--dry-run"])

        compose_rationales.main()  # would raise SystemExit if it tried to exit non-zero
