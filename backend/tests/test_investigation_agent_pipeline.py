"""SCRUM-54: every terminal path of the Investigation Agent pipeline, from
seeded Postgres rows through scripts.compose_rationales to the persisted
agent_rationales row and what GET /transactions then serves. Fixtures come
from tests/agent_fixtures.py; tests assert on rows, outcomes and served
state, never on model wording.
"""

import sys

import pytest
from agent_fixtures import (
    FLAGGED_SCENARIOS,
    UNGROUNDED_TEXT,
    anthropic_error,
    completion,
    grounded_text,
    seed_scenario,
)
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.pregel import Pregel
from sqlalchemy import text

from app.database import SessionLocal, engine
from app.investigation_agent import fingerprint as fingerprint_module
from app.investigation_agent import graph as graph_module
from app.investigation_agent import llm as llm_module
from app.investigation_agent.fingerprint import compute_fact_fingerprint
from app.investigation_agent.payload import build_payload
from app.investigation_agent.prompts import MAX_RATIONALE_CHARS, PROMPT_VERSION
from app.investigation_agent.state import transaction_data_from_orm
from app.investigation_agent.validation import (
    MAX_VALIDATION_ATTEMPTS,
    VALIDATOR_VERSION,
)
from app.main import app
from app.models import AgentRationale, Transaction
from app.routers import transactions as transactions_router
from app.rules.engine import concatenate_rationales, evaluate_all_rules
from scripts import compose_rationales

client = TestClient(app)


def _run_script(monkeypatch, user_id: int) -> int:
    monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(user_id)])
    try:
        compose_rationales.main()
    except SystemExit as exc:
        return exc.code
    return 0


def _rows(transaction_id: int) -> list[AgentRationale]:
    db = SessionLocal()
    try:
        return db.query(AgentRationale).filter(AgentRationale.transaction_id == transaction_id).order_by(AgentRationale.id).all()
    finally:
        db.close()


def _expected_fingerprint(seeded, transaction_id: int) -> str:
    db = SessionLocal()
    try:
        snapshot = transaction_data_from_orm(db.get(Transaction, transaction_id))
    finally:
        db.close()
    return compute_fact_fingerprint(
        build_payload(snapshot, seeded.rule_names_by_id[transaction_id], seeded.rule_values_by_id[transaction_id])
    )


def _interim_rationale(seeded, transaction_id: int) -> str:
    db = SessionLocal()
    try:
        transactions = db.query(Transaction).filter(Transaction.user_id == seeded.user_id).all()
        return concatenate_rationales(evaluate_all_rules(transactions))[transaction_id]
    finally:
        db.close()


def _served(user_id: int) -> dict[int, dict]:
    response = client.get("/transactions", params={"user_id": user_id, "limit": 200})
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()["items"]}


def _attempts_used(user_id: int, transaction_id: int) -> int | None:
    """The script's own view of the validation-failure budget for one
    transaction: attempts used if it's pending, None if it's skipped."""
    db = SessionLocal()
    try:
        pending, _passing, _capped = compose_rationales._collect_pending(db, user_id)
    finally:
        db.close()
    return next((p.attempts_used for p in pending if p.transaction.id == transaction_id), None)


# --- Pass ------------------------------------------------------------------


@pytest.mark.parametrize("name", FLAGGED_SCENARIOS)
def test_passing_composition_is_persisted_and_served_for_every_rule_type(name, monkeypatch, scripted_llm):
    """Writer/reader parity: the row the script writes is the row GET serves,
    so both sides computed the same fingerprint for every flagged transaction."""
    seeded = seed_scenario(name)
    llm = scripted_llm(*[completion("grounded", seeded)] * len(seeded.flagged_ids))

    assert _run_script(monkeypatch, seeded.user_id) == 0

    assert llm.invocations == len(seeded.flagged_ids)
    served = _served(seeded.user_id)
    for transaction_id in seeded.flagged_ids:
        [row] = _rows(transaction_id)
        assert row.validation_passed is True
        assert row.rationale is not None and row.composed_text == row.rationale
        assert row.violations == []
        assert row.composition_error is None
        assert row.fact_fingerprint == _expected_fingerprint(seeded, transaction_id)
        assert (row.prompt_version, row.validator_version, row.model_id) == (PROMPT_VERSION, VALIDATOR_VERSION, "mock")
        assert served[transaction_id]["rationale"] == row.rationale
        assert served[transaction_id]["rule_names"] == list(seeded.scenario.expected_rules)


def test_extended_thinking_blocks_persist_only_the_text_block(monkeypatch, scripted_llm):
    seeded = seed_scenario("geographic_anomaly")
    scripted_llm(completion("thinking", seeded))

    _run_script(monkeypatch, seeded.user_id)

    [row] = _rows(seeded.primary_id)
    assert row.validation_passed is True
    assert row.composed_text == grounded_text(seeded)


# --- Validation failures and the retry budget -------------------------------


def test_validation_failure_is_persisted_and_interim_is_served(monkeypatch, scripted_llm):
    seeded = seed_scenario("geographic_anomaly")
    scripted_llm(completion("ungrounded", seeded))

    assert _run_script(monkeypatch, seeded.user_id) == 0

    [row] = _rows(seeded.primary_id)
    assert row.validation_passed is False
    assert row.rationale is None
    assert row.composed_text == UNGROUNDED_TEXT
    assert row.composition_error is None
    assert {v["violation_type"] for v in row.violations} == {"unsupported_entity"}
    assert _attempts_used(seeded.user_id, seeded.primary_id) == 1
    assert _served(seeded.user_id)[seeded.primary_id]["rationale"] == _interim_rationale(seeded, seeded.primary_id)


def test_over_length_rationale_is_a_validation_failure_not_a_truncation(monkeypatch, scripted_llm):
    seeded = seed_scenario("new_merchant_risk")
    too_long = completion("too_long", seeded)
    scripted_llm(too_long)

    _run_script(monkeypatch, seeded.user_id)

    [row] = _rows(seeded.primary_id)
    assert row.validation_passed is False
    assert [v["violation_type"] for v in row.violations] == ["rationale_too_long"]
    assert row.composed_text == too_long.content and len(row.composed_text) > MAX_RATIONALE_CHARS
    assert _attempts_used(seeded.user_id, seeded.primary_id) == 1
    assert _served(seeded.user_id)[seeded.primary_id]["rationale"] == _interim_rationale(seeded, seeded.primary_id)


def test_fail_then_pass_on_the_second_attempt_is_served(monkeypatch, scripted_llm):
    seeded = seed_scenario("velocity")
    others = len(seeded.flagged_ids) - 1
    # Oldest-first: the primary (last burst member) is composed last each run.
    scripted_llm(*[completion("grounded", seeded)] * others, completion("ungrounded", seeded))
    _run_script(monkeypatch, seeded.user_id)
    assert _attempts_used(seeded.user_id, seeded.primary_id) == 1

    llm = scripted_llm(completion("grounded", seeded))
    _run_script(monkeypatch, seeded.user_id)
    assert llm.invocations == 1  # only the primary was still pending

    failed, passed = _rows(seeded.primary_id)
    assert (failed.validation_passed, passed.validation_passed) == (False, True)
    assert _served(seeded.user_id)[seeded.primary_id]["rationale"] == passed.rationale

    idle = scripted_llm()
    _run_script(monkeypatch, seeded.user_id)
    assert idle.invocations == 0
    assert len(_rows(seeded.primary_id)) == 2


def _patch_prompt_version(monkeypatch, version: str) -> None:
    # Every module that imported PROMPT_VERSION by name, plus fingerprint.py,
    # which reads its module global when hashing.
    for module in (compose_rationales, fingerprint_module, transactions_router):
        monkeypatch.setattr(module, "PROMPT_VERSION", version)


def test_prompt_version_bump_resets_the_validation_budget(monkeypatch, scripted_llm):
    seeded = seed_scenario("amount_deviation")
    for _ in range(MAX_VALIDATION_ATTEMPTS):
        scripted_llm(completion("ungrounded", seeded))
        _run_script(monkeypatch, seeded.user_id)
    assert _attempts_used(seeded.user_id, seeded.primary_id) is None  # capped
    capped = scripted_llm()
    _run_script(monkeypatch, seeded.user_id)
    assert capped.invocations == 0

    _patch_prompt_version(monkeypatch, "v99-test")
    assert _attempts_used(seeded.user_id, seeded.primary_id) == 0
    scripted_llm(completion("grounded", seeded))
    _run_script(monkeypatch, seeded.user_id)

    *old, new = _rows(seeded.primary_id)
    assert len(old) == MAX_VALIDATION_ATTEMPTS
    assert (new.prompt_version, new.validation_passed) == ("v99-test", True)
    assert new.fact_fingerprint != old[0].fact_fingerprint


def test_prompt_version_bump_recomposes_an_already_passing_transaction(monkeypatch, scripted_llm):
    seeded = seed_scenario("geographic_anomaly")
    scripted_llm(completion("grounded", seeded))
    _run_script(monkeypatch, seeded.user_id)
    idle = scripted_llm()
    _run_script(monkeypatch, seeded.user_id)
    assert idle.invocations == 0

    _patch_prompt_version(monkeypatch, "v99-test")
    # Under the new version nothing passing exists yet: GET falls back to interim.
    assert _served(seeded.user_id)[seeded.primary_id]["rationale"] == _interim_rationale(seeded, seeded.primary_id)
    llm = scripted_llm(completion("grounded", seeded))
    _run_script(monkeypatch, seeded.user_id)

    assert llm.invocations == 1
    old, new = _rows(seeded.primary_id)
    assert (old.prompt_version, new.prompt_version) == (PROMPT_VERSION, "v99-test")
    assert _served(seeded.user_id)[seeded.primary_id]["rationale"] == new.rationale


# --- Tool and model failures ------------------------------------------------


def _assert_composition_error_row(row, prefix: str) -> None:
    assert row.composition_error.startswith(prefix), row.composition_error
    assert row.validation_passed is False
    assert row.rationale is None
    assert row.composed_text is None
    assert row.violations == []
    assert row.validator_version == VALIDATOR_VERSION


def test_tool_error_records_a_tool_error_without_calling_the_model(monkeypatch, scripted_llm):
    seeded = seed_scenario("meridian_triple")

    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated get_geo_distance failure")

    monkeypatch.setattr(graph_module.get_geo_distance, "func", _raise)
    llm = scripted_llm()

    assert _run_script(monkeypatch, seeded.user_id) == 1

    assert llm.invocations == 0
    [row] = _rows(seeded.primary_id)
    _assert_composition_error_row(row, "ToolError[get_geo_distance]: RuntimeError: ")
    assert "get_merchant_risk_score" not in row.composition_error  # the other parallel tool succeeded
    assert _attempts_used(seeded.user_id, seeded.primary_id) == 0  # no budget used
    assert _served(seeded.user_id)[seeded.primary_id]["rationale"] == _interim_rationale(seeded, seeded.primary_id)


def test_statement_timeout_inside_a_tool_is_recorded_as_a_tool_error(monkeypatch, scripted_llm):
    """SCRUM-56's Postgres statement_timeout on tool sessions, actually fired:
    a second connection holds ACCESS EXCLUSIVE on transactions, so the tool's
    SELECT waits until its 200ms timeout cancels it (QueryCanceled).

    Drives the script's own _collect_pending/_compose_one rather than main():
    main() keeps its session's transaction (and so an ACCESS SHARE lock on
    transactions) open across composition, which the lock below would wait
    on. The row is persisted exactly as main() does (add + commit)."""
    seeded = seed_scenario("geographic_anomaly")
    llm = scripted_llm()
    monkeypatch.setenv("INVESTIGATION_AGENT_TOOL_DB_TIMEOUT_MS", "200")

    db = SessionLocal()
    try:
        [pending], _, _ = compose_rationales._collect_pending(db, seeded.user_id)
    finally:
        db.close()  # releases its locks; the loaded attributes stay usable

    lock_conn = engine.connect()
    try:
        lock_conn.execute(text("SET lock_timeout = '2s'"))
        lock_conn.execute(text("LOCK TABLE transactions IN ACCESS EXCLUSIVE MODE"))
        row, label = compose_rationales._compose_one(pending)
    finally:
        lock_conn.rollback()
        lock_conn.close()

    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
    finally:
        db.close()

    assert llm.invocations == 0
    assert label == "ToolError[get_geo_distance]:OperationalError"
    [row] = _rows(seeded.primary_id)
    _assert_composition_error_row(row, "ToolError[get_geo_distance]: OperationalError: ")
    assert "QueryCanceled" in row.composition_error
    assert _attempts_used(seeded.user_id, seeded.primary_id) == 0


@pytest.mark.parametrize(
    ("kind", "expected_prefix"),
    [
        ("server_error", "ModelError: InternalServerError(500): "),
        ("rate_limit", "ModelError: RateLimitError(429): "),
        ("overloaded", "ModelError: OverloadedError(529): "),
        ("timeout", "ModelError: APITimeoutError(-): "),
    ],
)
def test_model_error_records_class_and_status_without_using_the_budget(kind, expected_prefix, monkeypatch, scripted_llm):
    seeded = seed_scenario("amount_deviation")
    llm = scripted_llm(anthropic_error(kind))

    assert _run_script(monkeypatch, seeded.user_id) == 1

    assert llm.invocations == 1
    [row] = _rows(seeded.primary_id)
    _assert_composition_error_row(row, expected_prefix)
    assert _attempts_used(seeded.user_id, seeded.primary_id) == 0


# --- GET /transactions is read-only -----------------------------------------


@pytest.fixture(params=["miss", "hit"])
def served_scenario(request, monkeypatch, scripted_llm):
    """A flagged user with no agent_rationales row ("miss") or with a passing
    one the script wrote ("hit"). Returns the seeded scenario."""
    seeded = seed_scenario("meridian_triple")
    if request.param == "hit":
        scripted_llm(completion("grounded", seeded))
        _run_script(monkeypatch, seeded.user_id)
        assert _rows(seeded.primary_id)[0].validation_passed is True
    return seeded


def test_get_makes_zero_model_calls_and_zero_graph_invocations(served_scenario, monkeypatch):
    """Patched at the class level -- every chat model's invoke(), every
    compiled graph's invoke()/stream() -- not at llm.get_chat_model, which
    graph.py binds by name at import (the replaced test patched only that,
    so it could never fail)."""
    calls = []

    def _recording(name, original):
        def wrapper(self, *args, **kwargs):
            calls.append(name)
            return original(self, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(BaseChatModel, "invoke", _recording("model.invoke", BaseChatModel.invoke))
    monkeypatch.setattr(Pregel, "invoke", _recording("graph.invoke", Pregel.invoke))
    monkeypatch.setattr(Pregel, "stream", _recording("graph.stream", Pregel.stream))
    for module in (llm_module, graph_module):
        original = module.get_chat_model
        monkeypatch.setattr(module, "get_chat_model", lambda *a, _o=original, **k: calls.append("get_chat_model") or _o(*a, **k))

    _served(served_scenario.user_id)

    assert calls == []


_READ_ONLY_TABLES = ("users", "transactions", "agent_rationales")


def _snapshot() -> dict[str, list]:
    with engine.connect() as conn:
        return {table: conn.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() for table in _READ_ONLY_TABLES}


def test_get_makes_zero_writes_outside_transaction_flags(served_scenario):
    before = _snapshot()

    _served(served_scenario.user_id)
    _served(served_scenario.user_id)

    assert _snapshot() == before
