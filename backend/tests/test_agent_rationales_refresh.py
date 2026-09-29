"""SCRUM-53 Phase B: app/routers/transactions.py's _refresh_flags reads
agent_rationales (via _lookup_agent_rationales) but never writes to it and
never calls a model -- composition only ever happens in
scripts.compose_rationales. Own TestClient + in-memory SQLite engine, same
pattern as tests/test_transactions.py, kept in a separate file so these
SCRUM-53 Phase B cases don't get lost among that file's SCRUM-61 cases.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.investigation_agent import llm as llm_module
from app.investigation_agent.fingerprint import compute_fact_fingerprint
from app.investigation_agent.payload import build_payload
from app.investigation_agent.prompts import PROMPT_VERSION
from app.investigation_agent.state import transaction_data_from_orm
from app.main import app
from app.models import AgentRationale, Transaction, User
from app.rules.engine import (
    evaluate_all_rules,
    rule_names_by_transaction,
    rule_values_by_transaction,
)

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


client = TestClient(app)


@pytest.fixture(autouse=True)
def db_schema():
    """Also swaps app.dependency_overrides[get_db] in for the duration of
    each test, restoring whatever was there before -- tests/test_transactions.py
    sets its OWN override at module scope (against its own, different
    engine), and since app.dependency_overrides is a plain dict on the
    shared FastAPI `app` singleton, whichever file's module-level assignment
    happens to run last at collection time would otherwise silently win for
    every test in the process, regardless of which file is executing."""
    Base.metadata.create_all(bind=engine)
    previous_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    yield
    if previous_override is not None:
        app.dependency_overrides[get_db] = previous_override
    else:
        app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=engine)


def _fingerprint_for(all_transactions: list[Transaction], outlier_id: int) -> str:
    outlier = next(t for t in all_transactions if t.id == outlier_id)
    hits = evaluate_all_rules(all_transactions)
    rule_names_by_id = rule_names_by_transaction(hits)
    rule_values_by_id = rule_values_by_transaction(hits)
    payload = build_payload(
        transaction_data_from_orm(outlier),
        rule_names_by_id[outlier_id],
        rule_values_by_id.get(outlier_id, {}),
    )
    return compute_fact_fingerprint(payload)


def _seed_flagged_transaction() -> tuple[int, int, str]:
    """A user with a single amount_deviation outlier (same shape as
    test_transactions.py's own flagged-transaction fixture) -- returns
    (user_id, outlier's transaction id, that transaction's CURRENT fact
    fingerprint) so a test can insert an agent_rationales row that either
    does or doesn't match it.
    """
    db = TestingSessionLocal()
    user = User(name="Agent Rationale User")
    db.add(user)
    db.flush()
    history = [
        Transaction(
            user_id=user.id,
            timestamp=datetime.now(timezone.utc) - timedelta(days=10 - i),
            merchant="Test Merchant",
            category="groceries",
            amount=Decimal("10.00"),
            latitude=47.6062,
            longitude=-122.3321,
            location_label="Seattle, WA",
        )
        for i in range(5)
    ]
    outlier = Transaction(
        user_id=user.id,
        timestamp=datetime.now(timezone.utc),
        merchant="Test Merchant",
        category="groceries",
        amount=Decimal("500.00"),
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )
    db.add_all([*history, outlier])
    db.commit()
    user_id, outlier_id = user.id, outlier.id
    all_transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
    fingerprint = _fingerprint_for(all_transactions, outlier_id)
    db.close()
    return user_id, outlier_id, fingerprint


def _insert_agent_rationale(**overrides) -> None:
    defaults = {
        "prompt_version": PROMPT_VERSION,
        "model_id": "mock",
        "rationale": "This $500.00 purchase was flagged by the Investigation Agent.",
        "validation_passed": True,
        "violations": [],
        "composition_error": None,
    }
    defaults.update(overrides)
    db = TestingSessionLocal()
    db.add(AgentRationale(**defaults))
    db.commit()
    db.close()


def test_hit_serves_the_cached_agent_rationale():
    user_id, outlier_id, fingerprint = _seed_flagged_transaction()
    _insert_agent_rationale(transaction_id=outlier_id, fact_fingerprint=fingerprint)

    response = client.get("/transactions", params={"user_id": user_id})
    assert response.status_code == 200
    items_by_id = {item["id"]: item for item in response.json()["items"]}

    assert items_by_id[outlier_id]["rationale"] == "This $500.00 purchase was flagged by the Investigation Agent."
    assert items_by_id[outlier_id]["is_flagged"] is True


def test_miss_serves_the_interim_rationale():
    user_id, outlier_id, _fingerprint = _seed_flagged_transaction()
    # No agent_rationales row at all for this transaction/fingerprint.

    response = client.get("/transactions", params={"user_id": user_id})
    items_by_id = {item["id"]: item for item in response.json()["items"]}

    assert "higher than your typical spend" in items_by_id[outlier_id]["rationale"]


def test_failed_only_row_serves_the_interim_rationale():
    user_id, outlier_id, fingerprint = _seed_flagged_transaction()
    _insert_agent_rationale(
        transaction_id=outlier_id,
        fact_fingerprint=fingerprint,
        validation_passed=False,
        rationale=None,
        violations=[{"span": "", "violation_type": "empty_rationale", "reason": "Rationale is empty."}],
        composition_error="simulated model timeout",
    )

    response = client.get("/transactions", params={"user_id": user_id})
    items_by_id = {item["id"]: item for item in response.json()["items"]}

    assert "higher than your typical spend" in items_by_id[outlier_id]["rationale"]


def test_failed_row_with_composed_text_still_serves_interim_never_the_failed_text():
    """SCRUM-53 follow-up: composed_text (unlike rationale) IS populated on
    a failed row -- it's the audit record of what the model actually wrote,
    deliberately kept out of reach of the serving path (see
    app.models.AgentRationale's own docstring on why these are two separate
    columns). This is the direct regression test for that: a failed row
    whose composed_text holds real, distinctive, ungrounded prose must never
    have that text appear in the API response -- only the interim
    formatter's rationale, exactly as if composed_text didn't exist at all.
    """
    user_id, outlier_id, fingerprint = _seed_flagged_transaction()
    leaked_marker = "This looks like it happened in Wakanda, which is unusual."
    _insert_agent_rationale(
        transaction_id=outlier_id,
        fact_fingerprint=fingerprint,
        validation_passed=False,
        rationale=None,
        composed_text=leaked_marker,
        validator_version="v3",
        violations=[{"span": "Wakanda", "violation_type": "unsupported_entity", "reason": "not citable"}],
    )

    response = client.get("/transactions", params={"user_id": user_id})
    body = response.text
    items_by_id = {item["id"]: item for item in response.json()["items"]}

    assert leaked_marker not in body
    assert "higher than your typical spend" in items_by_id[outlier_id]["rationale"]


def test_prompt_version_mismatch_serves_the_interim_rationale():
    user_id, outlier_id, fingerprint = _seed_flagged_transaction()
    _insert_agent_rationale(
        transaction_id=outlier_id,
        fact_fingerprint=fingerprint,
        prompt_version="v0-stale",
        rationale="A stale rationale from an old prompt version.",
    )

    response = client.get("/transactions", params={"user_id": user_id})
    items_by_id = {item["id"]: item for item in response.json()["items"]}

    assert "higher than your typical spend" in items_by_id[outlier_id]["rationale"]


def test_refresh_makes_zero_model_calls(monkeypatch):
    """Even on a guaranteed miss (no agent_rationales row at all), GET
    /transactions must never construct a chat model -- composition only
    ever happens in scripts.compose_rationales."""

    def _raise():
        raise AssertionError("GET /transactions must never construct a chat model")

    monkeypatch.setattr(llm_module, "get_chat_model", _raise)
    user_id, _outlier_id, _fingerprint = _seed_flagged_transaction()

    response = client.get("/transactions", params={"user_id": user_id})

    assert response.status_code == 200


def test_refresh_makes_zero_writes_to_agent_rationales():
    user_id, outlier_id, fingerprint = _seed_flagged_transaction()
    _insert_agent_rationale(transaction_id=outlier_id, fact_fingerprint=fingerprint)

    db = TestingSessionLocal()
    before_count = db.query(AgentRationale).count()
    db.close()

    client.get("/transactions", params={"user_id": user_id})
    client.get("/transactions", params={"user_id": user_id})

    db = TestingSessionLocal()
    after_count = db.query(AgentRationale).count()
    db.close()

    assert after_count == before_count
