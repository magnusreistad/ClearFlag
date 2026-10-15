"""SCRUM-29: POST /transactions/{id}/disposition and the current disposition
GET /transactions shows. Own TestClient, same pattern as
tests/test_transactions.py.

The case this file exists for: _refresh_flags deletes and reinserts every
TransactionFlag row on every GET, and a disposition must survive that --
see test_disposition_survives_a_flag_refresh.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.database import SessionLocal as TestingSessionLocal
from app.database import engine
from app.main import app
from app.models import (
    DISPOSITION_NOTE_MAX_LENGTH,
    DISPOSITION_VALUES,
    Transaction,
    TransactionDisposition,
    TransactionFlag,
    User,
)
from app.schemas import DispositionValue

client = TestClient(app)


def make_transaction(user_id: int, days_ago: int, amount: Decimal) -> Transaction:
    return Transaction(
        user_id=user_id,
        timestamp=datetime.now(UTC) - timedelta(days=days_ago),
        merchant="Test Merchant",
        category="groceries",
        amount=amount,
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )


def seed_user_with_outlier(name: str = "Disposition User") -> tuple[int, int, int]:
    """Five $10 groceries purchases and one $500 outlier, which
    amount_deviation flags. Returns (user_id, outlier_id, an unflagged
    transaction's id). Nothing is flagged in the database until a GET
    /transactions runs the refresh."""
    db = TestingSessionLocal()
    user = User(name=name)
    db.add(user)
    db.flush()
    history = [
        make_transaction(user.id, days_ago=10 - i, amount=Decimal("10.00"))
        for i in range(5)
    ]
    outlier = make_transaction(user.id, days_ago=0, amount=Decimal("500.00"))
    db.add_all([*history, outlier])
    db.commit()
    ids = (user.id, outlier.id, history[0].id)
    db.close()
    return ids


def refresh(user_id: int, **params) -> dict[int, dict]:
    response = client.get("/transactions", params={"user_id": user_id, **params})
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()["items"]}


def post_disposition(transaction_id: int, user_id: int, **body):
    return client.post(
        f"/transactions/{transaction_id}/disposition",
        params={"user_id": user_id},
        json={"disposition": "false_positive", **body},
    )


def disposition_rows(transaction_id: int | None = None) -> list[TransactionDisposition]:
    db = TestingSessionLocal()
    query = db.query(TransactionDisposition)
    if transaction_id is not None:
        query = query.filter(TransactionDisposition.transaction_id == transaction_id)
    rows = query.order_by(TransactionDisposition.id).all()
    db.close()
    return rows


def flag_ids(transaction_id: int) -> set[int]:
    db = TestingSessionLocal()
    ids = {
        flag_id
        for (flag_id,) in db.query(TransactionFlag.id).filter(
            TransactionFlag.transaction_id == transaction_id
        )
    }
    db.close()
    return ids


class StatementLog:
    """Every SQL statement the app's engine executes while active."""

    def __init__(self):
        self.statements: list[str] = []

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._record)

    def touching_dispositions(self) -> list[str]:
        return [s for s in self.statements if "transaction_dispositions" in s]


# --- POST --------------------------------------------------------------------


def test_schema_disposition_values_match_the_model():
    assert get_args(DispositionValue) == DISPOSITION_VALUES


def test_post_creates_a_disposition_with_the_flags_rule_names_snapshot():
    user_id, outlier_id, _ = seed_user_with_outlier()
    items = refresh(user_id)

    response = post_disposition(
        outlier_id, user_id, disposition="confirmed_fraud", note="Customer confirmed."
    )

    assert response.status_code == 201
    body = response.json()
    assert body["transaction_id"] == outlier_id
    assert body["disposition"] == "confirmed_fraud"
    assert body["note"] == "Customer confirmed."
    assert body["rule_names"] == items[outlier_id]["rule_names"] == ["amount_deviation"]
    assert body["created_at"] is not None

    [row] = disposition_rows(outlier_id)
    assert row.id == body["id"]
    assert row.rule_names == ["amount_deviation"]


def test_post_unknown_transaction_is_404():
    user_id, _, _ = seed_user_with_outlier()

    response = post_disposition(999_999, user_id)

    assert response.status_code == 404
    assert disposition_rows() == []


def test_post_another_users_transaction_is_404():
    owner_id, outlier_id, _ = seed_user_with_outlier("Owner")
    other_id, _, _ = seed_user_with_outlier("Someone Else")
    refresh(owner_id)

    response = post_disposition(outlier_id, other_id)

    assert response.status_code == 404
    assert disposition_rows() == []


def test_post_unflagged_transaction_is_409():
    user_id, _, unflagged_id = seed_user_with_outlier()
    refresh(user_id)

    response = post_disposition(unflagged_id, user_id)

    assert response.status_code == 409
    assert disposition_rows() == []


def test_post_before_any_flag_refresh_is_409():
    """The 409 check reads persisted flags, never re-runs the rules -- so a
    transaction the rules WOULD flag, but no GET has refreshed yet, has no
    active flags to judge."""
    user_id, outlier_id, _ = seed_user_with_outlier()

    response = post_disposition(outlier_id, user_id)

    assert response.status_code == 409
    assert disposition_rows() == []


@pytest.mark.parametrize(
    ("params", "body"),
    [
        pytest.param(
            {"user_id": "USER"},
            {"disposition": "maybe_fraud"},
            id="unknown-disposition",
        ),
        pytest.param({"user_id": "USER"}, {}, id="missing-disposition"),
        pytest.param(
            {"user_id": "USER"},
            {"disposition": "ignored", "note": "x" * (DISPOSITION_NOTE_MAX_LENGTH + 1)},
            id="note-too-long",
        ),
        pytest.param({}, {"disposition": "ignored"}, id="missing-user-id"),
    ],
)
def test_post_invalid_request_is_422(params, body):
    user_id, outlier_id, _ = seed_user_with_outlier()
    refresh(user_id)
    params = {
        key: user_id if value == "USER" else value for key, value in params.items()
    }

    response = client.post(
        f"/transactions/{outlier_id}/disposition", params=params, json=body
    )

    assert response.status_code == 422
    assert disposition_rows() == []


def test_post_accepts_a_note_at_the_max_length():
    user_id, outlier_id, _ = seed_user_with_outlier()
    refresh(user_id)

    response = post_disposition(
        outlier_id, user_id, note="x" * DISPOSITION_NOTE_MAX_LENGTH
    )

    assert response.status_code == 201
    assert len(disposition_rows(outlier_id)[0].note) == DISPOSITION_NOTE_MAX_LENGTH


@pytest.mark.parametrize("note", ["", "   ", "\n\t "])
def test_whitespace_only_note_is_stored_as_null(note):
    user_id, outlier_id, _ = seed_user_with_outlier()
    refresh(user_id)

    response = post_disposition(outlier_id, user_id, note=note)

    assert response.status_code == 201
    assert response.json()["note"] is None
    assert disposition_rows(outlier_id)[0].note is None


# --- current disposition on GET ---------------------------------------------


def test_get_shows_null_disposition_when_never_dispositioned():
    user_id, _, _ = seed_user_with_outlier()

    items = refresh(user_id)

    assert all(item["disposition"] is None for item in items.values())


def test_repeated_posts_append_and_get_shows_the_latest():
    user_id, outlier_id, _ = seed_user_with_outlier()
    refresh(user_id)

    post_disposition(outlier_id, user_id, disposition="false_positive")
    post_disposition(
        outlier_id, user_id, disposition="confirmed_fraud", note="Second look."
    )

    assert [row.disposition for row in disposition_rows(outlier_id)] == [
        "false_positive",
        "confirmed_fraud",
    ]
    disposition = refresh(user_id)[outlier_id]["disposition"]
    assert disposition["value"] == "confirmed_fraud"
    assert disposition["note"] == "Second look."
    assert disposition["created_at"] is not None


def test_latest_wins_on_a_created_at_tie_by_id():
    """Two rows written in one database transaction share now(), so
    created_at alone can't order them -- id breaks the tie."""
    user_id, outlier_id, _ = seed_user_with_outlier()
    db = TestingSessionLocal()
    db.add(
        TransactionDisposition(
            transaction_id=outlier_id,
            disposition="ignored",
            rule_names=["amount_deviation"],
        )
    )
    db.flush()
    db.add(
        TransactionDisposition(
            transaction_id=outlier_id,
            disposition="false_positive",
            rule_names=["amount_deviation"],
        )
    )
    db.commit()
    db.close()
    first, second = disposition_rows(outlier_id)
    assert first.created_at == second.created_at

    assert refresh(user_id)[outlier_id]["disposition"]["value"] == "false_positive"


def test_disposition_survives_a_flag_refresh():
    user_id, outlier_id, _ = seed_user_with_outlier()
    refresh(user_id)
    post_disposition(
        outlier_id, user_id, disposition="false_positive", note="Known big shop."
    )
    flag_ids_before = flag_ids(outlier_id)
    rows_before = [
        (row.id, row.disposition, row.note, row.rule_names)
        for row in disposition_rows()
    ]

    items = refresh(user_id)

    # Proves the refresh really deleted and reinserted the flags -- without
    # this, a GET that skipped the refresh would pass trivially.
    flag_ids_after = flag_ids(outlier_id)
    assert flag_ids_after and flag_ids_after.isdisjoint(flag_ids_before)
    assert [
        (row.id, row.disposition, row.note, row.rule_names)
        for row in disposition_rows()
    ] == rows_before
    assert items[outlier_id]["disposition"]["value"] == "false_positive"
    assert items[outlier_id]["disposition"]["note"] == "Known big shop."


def test_disposition_and_its_rule_snapshot_survive_the_flag_disappearing():
    user_id, outlier_id, _ = seed_user_with_outlier()
    refresh(user_id)
    post_disposition(outlier_id, user_id, disposition="false_positive")

    db = TestingSessionLocal()
    db.query(Transaction).filter(Transaction.id == outlier_id).one().amount = Decimal(
        "10.00"
    )
    db.commit()
    db.close()
    items = refresh(user_id)

    assert items[outlier_id]["is_flagged"] is False
    assert flag_ids(outlier_id) == set()
    [row] = disposition_rows(outlier_id)
    assert row.rule_names == ["amount_deviation"]
    assert items[outlier_id]["disposition"]["value"] == "false_positive"


def test_get_uses_one_disposition_query_whatever_the_page_size():
    user_id, _, _ = seed_user_with_outlier()
    db = TestingSessionLocal()
    for transaction in db.query(Transaction).filter(Transaction.user_id == user_id):
        db.add(
            TransactionDisposition(
                transaction_id=transaction.id,
                disposition="ignored",
                rule_names=["velocity"],
            )
        )
    db.commit()
    db.close()

    with StatementLog() as one_item:
        small = refresh(user_id, limit=1)
    with StatementLog() as every_item:
        large = refresh(user_id, limit=50)

    assert len(small) == 1 and len(large) == 6
    assert all(item["disposition"] is not None for item in large.values())
    assert (
        len(one_item.touching_dispositions())
        == len(every_item.touching_dispositions())
        == 1
    )
    assert len(one_item.statements) == len(every_item.statements)


def test_get_never_writes_to_dispositions():
    user_id, outlier_id, _ = seed_user_with_outlier()
    refresh(user_id)
    post_disposition(outlier_id, user_id)
    rows_before = [
        (row.id, row.disposition, row.note, row.rule_names)
        for row in disposition_rows()
    ]

    with StatementLog() as log:
        refresh(user_id)
        refresh(user_id)

    writes = [
        s
        for s in log.touching_dispositions()
        if not s.lstrip().upper().startswith("SELECT")
    ]
    assert writes == []
    assert [
        (row.id, row.disposition, row.note, row.rule_names)
        for row in disposition_rows()
    ] == rows_before


def test_get_is_a_500_not_a_null_when_the_disposition_lookup_fails():
    """Decision 6: unlike the agent_rationales lookup, a failed disposition
    lookup must not degrade to disposition: null -- that would claim the
    transaction was never reviewed."""
    user_id, outlier_id, _ = seed_user_with_outlier()
    refresh(user_id)
    post_disposition(outlier_id, user_id)

    def fail_disposition_reads(
        conn, cursor, statement, parameters, context, executemany
    ):
        if statement.lstrip().startswith(
            "SELECT DISTINCT ON (transaction_dispositions"
        ):
            raise OperationalError(
                statement, parameters, Exception("simulated lookup failure")
            )

    event.listen(engine, "before_cursor_execute", fail_disposition_reads)
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/transactions", params={"user_id": user_id}
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_disposition_reads)

    assert response.status_code == 500


# --- database constraints ---------------------------------------------------
# test_schema_drift can't see CHECK constraints, so each one is exercised
# directly, bypassing the endpoint's own validation.


@pytest.mark.parametrize(
    ("column_values", "constraint"),
    [
        (
            "'maybe_fraud', NULL, ARRAY['velocity']",
            "ck_transaction_dispositions_disposition_value",
        ),
        (
            f"'ignored', repeat('x', {DISPOSITION_NOTE_MAX_LENGTH + 1}), ARRAY['velocity']",
            "ck_transaction_dispositions_note_length",
        ),
        (
            "'ignored', NULL, ARRAY[]::varchar[]",
            "ck_transaction_dispositions_rule_names_not_empty",
        ),
    ],
)
def test_database_check_constraints_reject_invalid_rows(column_values, constraint):
    _, outlier_id, _ = seed_user_with_outlier()

    with pytest.raises(IntegrityError, match=constraint), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO transaction_dispositions (transaction_id, disposition, note, rule_names) "
                f"VALUES (:transaction_id, {column_values})"
            ),
            {"transaction_id": outlier_id},
        )
