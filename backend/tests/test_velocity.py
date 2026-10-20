from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from app.database import engine
from app.models import Transaction, User
from app.rules.velocity import evaluate_velocity

# expire_on_commit=False: unlike test_transactions.py, these tests hand ORM
# objects straight to evaluate_velocity() after the session closes, so their
# attributes must stay readable without a live session to refresh from.
TestingSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
)


BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def make_transaction(
    user_id: int, timestamp: datetime, merchant: str = "Test Merchant"
) -> Transaction:
    return Transaction(
        user_id=user_id,
        timestamp=timestamp,
        merchant=merchant,
        category="groceries",
        amount=Decimal("10.00"),
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )


def new_user(db: Session) -> int:
    user = User(name="Velocity Test User")
    db.add(user)
    db.flush()
    return user.id


def create_transactions(
    db: Session, user_id: int, offsets_minutes: list[float]
) -> list[Transaction]:
    txns = [
        make_transaction(user_id, BASE_TIME + timedelta(minutes=m))
        for m in offsets_minutes
    ]
    db.add_all(txns)
    db.commit()
    return txns


def test_exact_max_count_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 2, 4, 6, 8])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert hits == []


def test_max_count_plus_one_flags_only_the_transaction_that_crosses_it():
    """SCRUM-76: the first five don't flag -- their own windows hold at most
    five -- only the sixth, whose window holds all six."""
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 2, 4, 6, 8, 10])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert [h.transaction_id for h in hits] == [txns[-1].id]
    assert "6 transactions" in hits[0].rationale


def test_burst_flags_from_the_crossing_transaction_on():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 1, 2, 3, 4, 5, 6])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert [h.transaction_id for h in hits] == [txns[5].id, txns[6].id]
    assert [h.values["transaction_count"] for h in hits] == [6, 7]


def test_each_hit_reports_its_own_windows_count_not_a_later_one():
    """SCRUM-76: the first six (offsets 0-5) cross the threshold at offset 5
    with 6; the 7th (offset 9) grows its own window to 7. Under the old
    look-ahead semantics offset 5 reported 7 -- now it reports only what
    its own window held."""
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 1, 2, 3, 4, 5, 9])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    hit_by_id = {h.transaction_id: h for h in hits}
    assert set(hit_by_id) == {txns[5].id, txns[6].id}
    assert "6 transactions" in hit_by_id[txns[5].id].rationale
    assert "7 transactions" in hit_by_id[txns[6].id].rationale


def test_window_includes_its_start_boundary_and_same_timestamp_transactions():
    """Both bounds inclusive, as in get_transaction_history: offset 0 is
    exactly 10 minutes before offset 10, and the two offset-5 transactions
    each count the other."""
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 1, 2, 3, 5, 5, 10])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert {h.transaction_id: h.values["transaction_count"] for h in hits} == {
        txns[4].id: 6,
        txns[5].id: 6,
        txns[6].id: 7,
    }


def test_spread_out_transactions_no_false_positive():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 15, 30, 45, 60, 75])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert hits == []


def test_unsorted_input_still_correct():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 1, 2, 3, 4, 5, 6])
    db.close()
    shuffled = list(reversed(txns))

    hits = evaluate_velocity(shuffled, max_count=5, window_minutes=10)

    assert [h.transaction_id for h in hits] == [txns[5].id, txns[6].id]
    assert [h.values["transaction_count"] for h in hits] == [6, 7]


def test_empty_transaction_list_returns_empty():
    assert evaluate_velocity([], max_count=5, window_minutes=10) == []


def test_single_transaction_returns_empty():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0])
    db.close()

    assert evaluate_velocity(txns, max_count=5, window_minutes=10) == []


def test_rationale_uses_actual_count_not_threshold():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 1, 2, 3, 4, 5, 6, 7])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert len(hits) == 3
    for h, expected in zip(hits, [6, 7, 8], strict=True):
        assert f"{expected} transactions" in h.rationale
        assert "5 transactions" not in h.rationale
        assert "in 10 minutes" in h.rationale


def test_rationale_is_byte_identical_to_the_pre_scrum_68_format():
    """SCRUM-68 snapshot: adding RuleHit.values must not change a single
    character of the rationale string this rule has always produced."""
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 2, 4, 6, 8, 10])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert hits[0].rationale == (
        "Flagged: You made 6 transactions in 10 minutes, which is unusual "
        "for your account."
    )


def test_values_populated_with_expected_keys_and_correct_figures():
    """SCRUM-68: values must carry exactly the two numbers the rationale
    string is built from."""
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 2, 4, 6, 8, 10])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    for h in hits:
        assert h.values == {"transaction_count": 6, "window_minutes": 10}
