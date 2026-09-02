from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Transaction, User
from app.rules.velocity import evaluate_velocity

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
# expire_on_commit=False: unlike test_transactions.py, these tests hand ORM
# objects straight to evaluate_velocity() after the session closes, so their
# attributes must stay readable without a live session to refresh from.
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def db_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_transaction(user_id: int, timestamp: datetime, merchant: str = "Test Merchant") -> Transaction:
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


def create_transactions(db: Session, user_id: int, offsets_minutes: list[float]) -> list[Transaction]:
    txns = [make_transaction(user_id, BASE_TIME + timedelta(minutes=m)) for m in offsets_minutes]
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


def test_max_count_plus_one_flags():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 2, 4, 6, 8, 10])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert {h.transaction_id for h in hits} == {t.id for t in txns}
    assert all("6 transactions" in h.rationale for h in hits)


def test_burst_larger_than_max_count_flags_every_member():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [0, 1, 2, 3, 4, 5, 6])
    db.close()

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    assert len(hits) == 7
    assert {h.transaction_id for h in hits} == {t.id for t in txns}
    assert all("7 transactions" in h.rationale for h in hits)


def test_transaction_in_multiple_windows_uses_largest_count():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # First 6 (offsets 0-5) alone form a count=6 window. Adding the 7th
    # (offset 9) still fits within 10 minutes of offset 0, growing that same
    # window to count=7 without the start pointer ever advancing -- so the
    # first transaction is swept up in both a 6-count and a 7-count window.
    txns = create_transactions(db, user_id, [0, 1, 2, 3, 4, 5, 9])
    db.close()
    first_txn = txns[0]

    hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

    hit_by_id = {h.transaction_id: h for h in hits}
    assert "7 transactions" in hit_by_id[first_txn.id].rationale
    assert "6 transactions" not in hit_by_id[first_txn.id].rationale


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

    assert len(hits) == 7
    assert {h.transaction_id for h in hits} == {t.id for t in txns}
    assert all("7 transactions" in h.rationale for h in hits)


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

    assert len(hits) == 8
    for h in hits:
        assert "8 transactions" in h.rationale
        assert "5 transactions" not in h.rationale
        assert "in 10 minutes" in h.rationale
