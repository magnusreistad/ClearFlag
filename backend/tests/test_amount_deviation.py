from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Transaction, User
from app.rules.amount_deviation import evaluate_amount_deviation

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
# expire_on_commit=False: unlike test_transactions.py, these tests hand ORM
# objects straight to evaluate_amount_deviation() after the session closes,
# so their attributes must stay readable without a live session to refresh
# from.
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def db_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_transaction(
    user_id: int,
    timestamp: datetime,
    amount: Decimal,
    category: str = "groceries",
    merchant: str = "Test Merchant",
) -> Transaction:
    return Transaction(
        user_id=user_id,
        timestamp=timestamp,
        merchant=merchant,
        category=category,
        amount=amount,
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )


def new_user(db: Session) -> int:
    user = User(name="Amount Deviation Test User")
    db.add(user)
    db.flush()
    return user.id


def create_transactions(
    db: Session,
    user_id: int,
    amounts: list[Decimal],
    category: str = "groceries",
) -> list[Transaction]:
    """One transaction per amount, spaced a day apart in list order (so
    creation order is also timestamp order)."""
    txns = [
        make_transaction(user_id, BASE_TIME + timedelta(days=i), amount, category=category)
        for i, amount in enumerate(amounts)
    ]
    db.add_all(txns)
    db.commit()
    return txns


HISTORY = [Decimal("60.00"), Decimal("55.00"), Decimal("70.00"), Decimal("65.00"), Decimal("58.00")]
# mean=61.60, sample stdev=~5.94 -- anything above 61.60 + 3*5.94 = ~79.42
# clears the default threshold.


def test_below_threshold_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [*HISTORY, Decimal("75.00")])
    db.close()

    hits = evaluate_amount_deviation(txns)

    assert hits == []


def test_above_threshold_flags():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [*HISTORY, Decimal("540.00")])
    db.close()

    hits = evaluate_amount_deviation(txns)

    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id
    assert "higher than your typical spend in this category" in hits[0].rationale


def test_rationale_reports_percentage_over_mean():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # mean=61.60 -> (540 - 61.6) / 61.6 * 100 ~= 776.6%, rounds to 777%
    txns = create_transactions(db, user_id, [*HISTORY, Decimal("540.00")])
    db.close()

    hits = evaluate_amount_deviation(txns)

    assert "777%" in hits[0].rationale


def test_too_few_historical_transactions_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # Only 4 prior transactions -- below MIN_HISTORY_COUNT of 5 -- however
    # extreme the 5th amount is.
    txns = create_transactions(db, user_id, [*HISTORY[:4], Decimal("999.00")])
    db.close()

    hits = evaluate_amount_deviation(txns)

    assert hits == []


def test_first_ever_transaction_in_category_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [Decimal("999.00")])
    db.close()

    hits = evaluate_amount_deviation(txns)

    assert hits == []


def test_zero_variance_history_does_not_explode_on_tiny_deviation():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # Five identical historical amounts (stdev=0), then a $1.50 bump --
    # small in absolute terms, but "infinite" stdevs without the floor.
    txns = create_transactions(db, user_id, [Decimal("9.99")] * 5 + [Decimal("11.49")])
    db.close()

    hits = evaluate_amount_deviation(txns)

    assert hits == []


def test_zero_variance_history_still_flags_large_deviation():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [Decimal("9.99")] * 5 + [Decimal("500.00")])
    db.close()

    hits = evaluate_amount_deviation(txns)

    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id


def test_unusually_low_amount_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [*HISTORY, Decimal("1.00")])
    db.close()

    hits = evaluate_amount_deviation(txns)

    assert hits == []


def test_categories_are_evaluated_independently():
    db = TestingSessionLocal()
    user_id = new_user(db)
    groceries = create_transactions(db, user_id, [*HISTORY, Decimal("540.00")], category="groceries")
    dining = create_transactions(db, user_id, HISTORY, category="dining")
    db.close()

    hits = evaluate_amount_deviation([*groceries, *dining])

    assert len(hits) == 1
    assert hits[0].transaction_id == groceries[-1].id


def test_unsorted_input_still_correct():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_transactions(db, user_id, [*HISTORY, Decimal("540.00")])
    db.close()
    shuffled = list(reversed(txns))

    hits = evaluate_amount_deviation(shuffled)

    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id


def test_empty_transaction_list_returns_empty():
    assert evaluate_amount_deviation([]) == []


def test_custom_threshold_narrows_what_flags():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # 75.00 sits well under the default 3-stdev bar (~79.42) but clears a
    # tighter 1-stdev bar (61.6 + 5.94 = ~67.54).
    txns = create_transactions(db, user_id, [*HISTORY, Decimal("75.00")])
    db.close()

    assert evaluate_amount_deviation(txns, std_dev_threshold=3) == []
    hits = evaluate_amount_deviation(txns, std_dev_threshold=1)
    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id
