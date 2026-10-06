"""SCRUM-53 Phase B / SCRUM-56: scripts.compose_rationales -- mock mode only,
no network. The script's own SessionLocal is app.database.SessionLocal,
which tests/conftest.py binds to TEST_DATABASE_URL, so it never touches the
real Neon DB.

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

import pytest
from agent_fixtures import UNGROUNDED_TEXT, add_amount_deviation_outlier, seed_scenario
from langchain_core.messages import AIMessage

from app.database import SessionLocal as TestingSessionLocal
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

GROUNDED_RATIONALE = (
    "This $500.00 purchase is far above your typical spend in this category, "
    "standing out clearly from your usual pattern."
)
UNGROUNDED_RATIONALE = UNGROUNDED_TEXT


def _messages(*texts: str) -> list[AIMessage]:
    return [AIMessage(content=text) for text in texts]


def _seed_flagged_user() -> tuple[int, int]:
    """SCRUM-54: the shared amount_deviation scenario (tests/agent_fixtures.py)."""
    seeded = seed_scenario("amount_deviation")
    return seeded.user_id, seeded.primary_id


class TestComposeAndSkip:
    def test_composes_a_pending_transaction_and_records_a_pass(self, monkeypatch, caplog, scripted_llm):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        scripted_llm(*_messages(GROUNDED_RATIONALE))
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

    def test_second_run_skips_the_already_passing_transaction(self, monkeypatch, caplog, scripted_llm):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        scripted_llm(*_messages(GROUNDED_RATIONALE))
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])
        compose_rationales.main()
        caplog.clear()  # discard first run's output

        # No further scripted responses at all: if the script tried to
        # compose again, the scripted_llm fixture would fail the test.
        compose_rationales.main()

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()
        assert len(rows) == 1  # not duplicated

        assert "run complete: passed=0 validation_failed=0 composition_error=0 skipped_capped=0 skipped_already_passing=1" in caplog.text
        assert f"transaction={outlier_id}" in caplog.text
        assert "outcome=skipped_already_passing" in caplog.text

    def test_records_a_failed_composition_without_crashing(self, monkeypatch, caplog, scripted_llm):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        scripted_llm(*_messages(UNGROUNDED_RATIONALE))
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
    def test_dry_run_makes_no_model_calls_and_no_writes(self, monkeypatch, capsys, scripted_llm):
        llm = scripted_llm()  # nothing scripted: any model call fails the test
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id), "--dry-run"])

        compose_rationales.main()

        assert llm.invocations == 0

        db = TestingSessionLocal()
        assert db.query(AgentRationale).count() == 0
        db.close()

        out = capsys.readouterr().out
        assert f"transaction {outlier_id}" in out
        assert "composed=0 passed=0 failed=0 skipped=0" in out


class TestLimit:
    def test_limit_bounds_how_many_are_composed_in_one_run(self, monkeypatch, caplog, scripted_llm):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        scripted_llm(*_messages(GROUNDED_RATIONALE, GROUNDED_RATIONALE))

        db = TestingSessionLocal()
        user = User(name="Limit User")
        db.add(user)
        db.flush()
        # Two independent amount-deviation outliers in two different
        # categories, so this user has two separate flagged transactions.
        add_amount_deviation_outlier(db, user.id, category="groceries")
        add_amount_deviation_outlier(db, user.id, category="dining")
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


class TestRetryCap:
    """SCRUM-56: MAX_VALIDATION_ATTEMPTS validation-failed attempts for a
    transaction's (transaction_id, fact_fingerprint, prompt_version,
    VALIDATOR_VERSION) budget key caps further composition -- but only
    validation failures consume that budget; composition_error attempts,
    and rows from a different budget key (a validator_version bump, or a
    row with validator_version IS NULL), don't count."""

    def test_cap_skips_the_third_attempt_without_calling_the_model(self, monkeypatch, caplog, scripted_llm):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        scripted_llm(*_messages(UNGROUNDED_RATIONALE))
        compose_rationales.main()
        scripted_llm(*_messages(UNGROUNDED_RATIONALE))
        compose_rationales.main()
        caplog.clear()

        counting_model = scripted_llm()
        compose_rationales.main()

        assert counting_model.invocations == 0  # the third run never even tries

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

    def test_composition_error_attempts_do_not_consume_the_budget(self, monkeypatch, caplog, scripted_llm):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        # Three runs, each a model failure -- if composition_error consumed
        # the same budget as a validation failure, the third would be capped
        # (MAX_VALIDATION_ATTEMPTS == 2) and never attempted at all. Each run
        # exits non-zero (SCRUM-56: any composition_error in a run does) --
        # that's exercised for its own sake in TestExitCode below, so it's
        # just caught and ignored here.
        for _ in range(3):
            scripted_llm(TimeoutError("simulated model timeout"))
            with pytest.raises(SystemExit) as exc_info:
                compose_rationales.main()
            assert exc_info.value.code == 1

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()
        assert len(rows) == 3
        assert all(row.composition_error is not None for row in rows)

        assert "outcome=skipped_capped" not in caplog.text

    def test_validator_version_bump_resets_the_budget(self, monkeypatch, caplog, scripted_llm):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
        user_id, outlier_id = _seed_flagged_user()
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        scripted_llm(*_messages(UNGROUNDED_RATIONALE))
        compose_rationales.main()
        scripted_llm(*_messages(UNGROUNDED_RATIONALE))
        compose_rationales.main()
        caplog.clear()

        # Confirm it really is capped under the current validator version first.
        counting_model = scripted_llm()
        compose_rationales.main()
        assert counting_model.invocations == 0
        caplog.clear()

        # A VALIDATOR_VERSION bump changes the budget key -- the two capped
        # attempts above no longer match it, so the budget is fresh again.
        monkeypatch.setattr(compose_rationales, "VALIDATOR_VERSION", "v99-test")
        scripted_llm(*_messages(GROUNDED_RATIONALE))
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

    def test_null_validator_version_row_does_not_consume_budget(self, monkeypatch, caplog, scripted_llm):
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
        scripted_llm(*_messages(GROUNDED_RATIONALE))

        compose_rationales.main()

        db = TestingSessionLocal()
        rows = db.query(AgentRationale).filter(AgentRationale.transaction_id == outlier_id).all()
        db.close()
        assert len(rows) == 2  # the legacy row, plus this run's new attempt
        assert "outcome=skipped_capped" not in caplog.text
        assert "outcome=passed" in caplog.text

    def test_capped_transactions_dont_count_against_limit(self, monkeypatch, caplog, scripted_llm):
        """Capping filters happen in _collect_pending, BEFORE --limit slices
        the pending list -- a capped transaction must not use up a run slot
        that a transaction still worth attempting could have had."""
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")

        db = TestingSessionLocal()
        user = User(name="Cap Limit User")
        db.add(user)
        db.flush()
        capped_id = add_amount_deviation_outlier(db, user.id, category="groceries")
        db.commit()
        user_id = user.id
        db.close()

        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])
        scripted_llm(*_messages(UNGROUNDED_RATIONALE))
        compose_rationales.main()
        scripted_llm(*_messages(UNGROUNDED_RATIONALE))
        compose_rationales.main()
        caplog.clear()

        # A second, fresh outlier for the same user, added after the first is capped.
        db = TestingSessionLocal()
        fresh_id = add_amount_deviation_outlier(db, user_id, category="dining")
        db.commit()
        db.close()

        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id), "--limit", "1"])
        scripted_llm(*_messages(GROUNDED_RATIONALE))
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
    def test_one_transactions_model_error_doesnt_stop_the_others_in_the_run(self, monkeypatch, caplog, scripted_llm):
        caplog.set_level(logging.INFO, logger="scripts.compose_rationales")

        db = TestingSessionLocal()
        user = User(name="Mixed Outcome User")
        db.add(user)
        db.flush()
        failing_id = add_amount_deviation_outlier(db, user.id, category="groceries")
        passing_id = add_amount_deviation_outlier(db, user.id, category="dining")
        db.commit()
        user_id = user.id
        db.close()

        # _collect_pending returns oldest-transaction-id-first, so the
        # groceries outlier (added first, lower id) is composed before the
        # dining one -- scripting the model to fail on the first call and
        # succeed on the second exercises exactly that ordering.
        scripted_llm(TimeoutError("simulated model timeout"), *_messages(GROUNDED_RATIONALE))
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
    def test_exits_zero_when_every_attempt_passes(self, monkeypatch, scripted_llm):
        user_id, _outlier_id = _seed_flagged_user()
        scripted_llm(*_messages(GROUNDED_RATIONALE))
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        compose_rationales.main()  # would raise SystemExit if it tried to exit non-zero

    def test_exits_non_zero_when_a_composition_error_occurs(self, monkeypatch, scripted_llm):
        user_id, _outlier_id = _seed_flagged_user()

        scripted_llm(TimeoutError("simulated model timeout"))
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])

        with pytest.raises(SystemExit) as exc_info:
            compose_rationales.main()
        assert exc_info.value.code == 1

    def test_dry_run_always_exits_zero_even_with_pending_work(self, monkeypatch, scripted_llm):
        user_id, _outlier_id = _seed_flagged_user()

        llm = scripted_llm()  # nothing scripted: any model call fails the test
        monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id), "--dry-run"])

        compose_rationales.main()  # would raise SystemExit if it tried to exit non-zero

        assert llm.invocations == 0
