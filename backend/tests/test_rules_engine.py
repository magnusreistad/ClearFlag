from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Transaction, User
from app.rules.engine import (
    FlagHit,
    concatenate_rationales,
    evaluate_all_rules,
    rule_names_by_transaction,
)

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
# expire_on_commit=False: like the individual rule tests, these hand ORM
# objects straight to evaluate_all_rules() after the session closes.
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
    amount: Decimal = Decimal("10.00"),
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
    user = User(name="Rules Engine Test User")
    db.add(user)
    db.flush()
    return user.id


def test_no_hits_returns_empty_list():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # Spread an hour apart (no velocity) at a constant amount (no deviation),
    # same merchant reused (no new-merchant), same location (no geo anomaly).
    txns = [make_transaction(user_id, BASE_TIME + timedelta(hours=i)) for i in range(4)]
    db.add_all(txns)
    db.commit()
    db.close()

    assert evaluate_all_rules(txns) == []
    assert concatenate_rationales([]) == {}


def test_single_rule_hit_is_tagged_with_its_rule_name():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # 5 identical-amount purchases establish a $10 category history an hour
    # apart (no velocity trigger), then a $500 outlier clears amount
    # deviation's 3-stdev bar. Same merchant throughout so new-merchant risk
    # never gets a second first-time purchase to fire on.
    history = [
        make_transaction(user_id, BASE_TIME + timedelta(hours=i), amount=Decimal("10.00")) for i in range(5)
    ]
    outlier = make_transaction(user_id, BASE_TIME + timedelta(hours=5), amount=Decimal("500.00"))
    txns = [*history, outlier]
    db.add_all(txns)
    db.commit()
    db.close()

    hits = evaluate_all_rules(txns)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.transaction_id == outlier.id
    assert hit.rule_name == "amount_deviation"
    assert "higher than your typical spend" in hit.rationale


def test_transaction_triggering_multiple_rules_gets_one_hit_per_rule():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # Mirrors test_velocity's test_max_count_plus_one_flags shape (6
    # transactions within 10 minutes trips velocity for all 6), but the 6th
    # is also an outlier against the $10 history the first 5 establish in
    # the same category -- so it trips amount deviation too.
    offsets = [0, 2, 4, 6, 8, 10]
    amounts = [Decimal("10.00")] * 5 + [Decimal("500.00")]
    txns = [
        make_transaction(user_id, BASE_TIME + timedelta(minutes=m), amount=a)
        for m, a in zip(offsets, amounts, strict=True)
    ]
    db.add_all(txns)
    db.commit()
    db.close()
    multi_hit_txn = txns[-1]

    hits = evaluate_all_rules(txns)

    hits_by_transaction: dict[int, list[FlagHit]] = {}
    for hit in hits:
        hits_by_transaction.setdefault(hit.transaction_id, []).append(hit)

    assert len(hits) == 7  # 6 velocity hits + 1 amount_deviation hit
    assert {h.rule_name for h in hits_by_transaction[multi_hit_txn.id]} == {
        "velocity",
        "amount_deviation",
    }
    for txn in txns[:-1]:
        assert {h.rule_name for h in hits_by_transaction[txn.id]} == {"velocity"}


def test_concatenate_rationales_merges_multi_rule_hits_by_transaction_id():
    hits = [
        FlagHit(transaction_id=1, rule_name="velocity", rationale="Flagged: velocity reason."),
        FlagHit(transaction_id=1, rule_name="amount_deviation", rationale="Flagged: amount reason."),
        FlagHit(transaction_id=2, rule_name="velocity", rationale="Flagged: velocity reason."),
    ]

    merged = concatenate_rationales(hits)

    assert merged == {
        1: "Flagged: velocity reason. Flagged: amount reason.",
        2: "Flagged: velocity reason.",
    }


def test_concatenate_rationales_empty_input_returns_empty_dict():
    assert concatenate_rationales([]) == {}


def test_rule_names_by_transaction_groups_multi_rule_hits_by_transaction_id():
    hits = [
        FlagHit(transaction_id=1, rule_name="velocity", rationale="Flagged: velocity reason."),
        FlagHit(transaction_id=1, rule_name="amount_deviation", rationale="Flagged: amount reason."),
        FlagHit(transaction_id=2, rule_name="velocity", rationale="Flagged: velocity reason."),
    ]

    grouped = rule_names_by_transaction(hits)

    assert grouped == {
        1: ["velocity", "amount_deviation"],
        2: ["velocity"],
    }


def test_rule_names_by_transaction_empty_input_returns_empty_dict():
    assert rule_names_by_transaction([]) == {}
