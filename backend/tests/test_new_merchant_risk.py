from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Transaction, User
from app.rules.new_merchant_risk import evaluate_new_merchant_risk

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
# expire_on_commit=False: unlike test_transactions.py, these tests hand ORM
# objects straight to evaluate_new_merchant_risk() after the session closes,
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
    merchant: str,
    amount: Decimal,
    category: str = "shopping",
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
    user = User(name="New Merchant Risk Test User")
    db.add(user)
    db.flush()
    return user.id


def create_first_time_purchases(db: Session, user_id: int, amounts: list[Decimal]) -> list[Transaction]:
    """One transaction per amount, each at its own distinct merchant, spaced
    a day apart in list order (so creation order is also timestamp order and
    every transaction is guaranteed to be a first-time purchase)."""
    txns = [
        make_transaction(user_id, BASE_TIME + timedelta(days=i), f"Merchant {i}", amount)
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
    txns = create_first_time_purchases(db, user_id, [*HISTORY, Decimal("75.00")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert hits == []


def test_above_threshold_flags():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_first_time_purchases(db, user_id, [*HISTORY, Decimal("540.00")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id
    assert "first purchase from this merchant" in hits[0].rationale
    assert "higher than your typical first-time purchase" in hits[0].rationale


def test_rationale_reports_percentage_over_mean():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # mean=61.60 -> (540 - 61.6) / 61.6 * 100 ~= 776.6%, rounds to 777%
    txns = create_first_time_purchases(db, user_id, [*HISTORY, Decimal("540.00")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert "777%" in hits[0].rationale


def test_too_few_historical_first_purchases_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # Only 4 prior first-time purchases -- below MIN_HISTORY_COUNT of 5 --
    # however extreme the 5th amount is.
    txns = create_first_time_purchases(db, user_id, [*HISTORY[:4], Decimal("999.00")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert hits == []


def test_exactly_one_prior_first_purchase_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # A single prior first-time purchase isn't enough for a meaningful
    # average/stdev -- must not flag or crash trying to compute stdev of one
    # sample.
    txns = create_first_time_purchases(db, user_id, [Decimal("50.00"), Decimal("999.00")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert hits == []


def test_very_first_transaction_overall_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_first_time_purchases(db, user_id, [Decimal("999.00")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert hits == []


def test_zero_variance_history_does_not_explode_on_tiny_deviation():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # Five identical first-purchase amounts at five distinct merchants
    # (stdev=0), then a $1.50 bump -- small in absolute terms, but
    # "infinite" stdevs without the floor.
    txns = create_first_time_purchases(db, user_id, [Decimal("9.99")] * 5 + [Decimal("11.49")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert hits == []


def test_zero_variance_history_still_flags_large_deviation():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_first_time_purchases(db, user_id, [Decimal("9.99")] * 5 + [Decimal("500.00")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id


def test_unusually_low_first_purchase_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_first_time_purchases(db, user_id, [*HISTORY, Decimal("1.00")])
    db.close()

    hits = evaluate_new_merchant_risk(txns)

    assert hits == []


def test_repeat_transaction_never_flags_regardless_of_amount():
    db = TestingSessionLocal()
    user_id = new_user(db)
    first_purchases = create_first_time_purchases(db, user_id, [*HISTORY, Decimal("62.00")])
    # A huge repeat purchase at an already-seen merchant -- must never flag,
    # no matter how far it would clear the threshold if it were (wrongly)
    # treated as a first-time purchase.
    repeat = make_transaction(
        user_id,
        BASE_TIME + timedelta(days=len(first_purchases)),
        first_purchases[0].merchant,
        Decimal("9999.00"),
    )
    db.add(repeat)
    db.commit()
    db.close()

    hits = evaluate_new_merchant_risk([*first_purchases, repeat])

    assert hits == []


def test_multiple_first_time_merchants_same_day_evaluated_independently():
    db = TestingSessionLocal()
    user_id = new_user(db)
    first_purchases = create_first_time_purchases(db, user_id, HISTORY)
    same_day = BASE_TIME + timedelta(days=len(HISTORY), hours=1)
    normal = make_transaction(user_id, same_day, "Normal New Merchant", Decimal("70.00"))
    outlier = make_transaction(user_id, same_day + timedelta(minutes=5), "Outlier New Merchant", Decimal("540.00"))
    db.add_all([normal, outlier])
    db.commit()
    db.close()

    hits = evaluate_new_merchant_risk([*first_purchases, normal, outlier])

    assert {h.transaction_id for h in hits} == {outlier.id}


def test_unsorted_input_still_correct():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = create_first_time_purchases(db, user_id, [*HISTORY, Decimal("540.00")])
    db.close()
    shuffled = list(reversed(txns))

    hits = evaluate_new_merchant_risk(shuffled)

    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id


def test_empty_transaction_list_returns_empty():
    assert evaluate_new_merchant_risk([]) == []


def test_custom_threshold_narrows_what_flags():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # 75.00 sits well under the default 3-stdev bar (~79.42) but clears a
    # tighter 1-stdev bar (61.6 + 5.94 = ~67.54).
    txns = create_first_time_purchases(db, user_id, [*HISTORY, Decimal("75.00")])
    db.close()

    assert evaluate_new_merchant_risk(txns, std_dev_threshold=3) == []
    hits = evaluate_new_merchant_risk(txns, std_dev_threshold=1)
    assert len(hits) == 1
    assert hits[0].transaction_id == txns[-1].id
