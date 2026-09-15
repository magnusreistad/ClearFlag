import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Transaction, TransactionFlag, User

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


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def db_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def make_transaction(
    user_id: int, days_ago: int, merchant: str = "Test Merchant", amount: Decimal = Decimal("10.00")
) -> Transaction:
    return Transaction(
        user_id=user_id,
        timestamp=datetime.now(timezone.utc) - timedelta(days=days_ago),
        merchant=merchant,
        category="groceries",
        amount=amount,
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )


def test_list_transactions_requires_user_id():
    response = client.get("/transactions")
    assert response.status_code == 422


def test_list_transactions_scoped_and_sorted_by_timestamp_desc():
    db = TestingSessionLocal()
    user_a = User(name="User A")
    user_b = User(name="User B")
    db.add_all([user_a, user_b])
    db.flush()

    db.add_all([
        make_transaction(user_a.id, days_ago=2, merchant="Older"),
        make_transaction(user_a.id, days_ago=0, merchant="Newest"),
        make_transaction(user_a.id, days_ago=1, merchant="Middle"),
        make_transaction(user_b.id, days_ago=0, merchant="Other user's transaction"),
    ])
    db.commit()
    user_a_id = user_a.id
    db.close()

    response = client.get("/transactions", params={"user_id": user_a_id})
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 3
    assert [item["merchant"] for item in body["items"]] == ["Newest", "Middle", "Older"]
    assert all(item["user_id"] == user_a_id for item in body["items"])


def test_list_transactions_pagination():
    db = TestingSessionLocal()
    user = User(name="Paginated User")
    db.add(user)
    db.flush()
    # days_ago=0 is newest, days_ago=4 is oldest; sorted desc: 0,1,2,3,4
    db.add_all(make_transaction(user.id, days_ago=i, merchant=f"Merchant {i}") for i in range(5))
    db.commit()
    user_id = user.id
    db.close()

    response = client.get("/transactions", params={"user_id": user_id, "limit": 2, "offset": 1})
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    # offset=1 skips "Merchant 0" (newest), returns the next 2 in desc order.
    assert [item["merchant"] for item in body["items"]] == ["Merchant 1", "Merchant 2"]


def test_list_transactions_rejects_limit_above_max():
    response = client.get("/transactions", params={"user_id": 1, "limit": 500})
    assert response.status_code == 422


def test_list_transactions_rejects_limit_below_min():
    response = client.get("/transactions", params={"user_id": 1, "limit": 0})
    assert response.status_code == 422


def test_flagged_transaction_surfaces_rationale_and_is_flagged():
    db = TestingSessionLocal()
    user = User(name="Flag Surfacing User")
    db.add(user)
    db.flush()
    # Same merchant throughout so new-merchant risk never gets a second
    # first-time purchase to fire on -- isolates this to amount deviation.
    history = [
        make_transaction(user.id, days_ago=10 - i, amount=Decimal("10.00")) for i in range(5)
    ]
    outlier = make_transaction(user.id, days_ago=0, amount=Decimal("500.00"))
    db.add_all([*history, outlier])
    db.commit()
    user_id = user.id
    outlier_id = outlier.id
    db.close()

    response = client.get("/transactions", params={"user_id": user_id})
    assert response.status_code == 200
    items_by_id = {item["id"]: item for item in response.json()["items"]}

    assert items_by_id[outlier_id]["is_flagged"] is True
    assert "higher than your typical spend" in items_by_id[outlier_id]["rationale"]
    assert items_by_id[outlier_id]["rule_names"] == ["amount_deviation"]
    for item_id, item in items_by_id.items():
        if item_id != outlier_id:
            assert item["is_flagged"] is False
            assert item["rationale"] is None
            assert item["rule_names"] == []


def test_transaction_triggering_multiple_rules_concatenates_rationale():
    db = TestingSessionLocal()
    user = User(name="Multi Rule Flag User")
    db.add(user)
    db.flush()
    base_time = datetime.now(timezone.utc) - timedelta(hours=1)
    # 6 transactions within 10 minutes trips velocity for all 6; the 6th is
    # also a $500 outlier against the $10 history the first 5 establish in
    # the same category, tripping amount deviation too.
    offsets = [0, 2, 4, 6, 8, 10]
    amounts = [Decimal("10.00")] * 5 + [Decimal("500.00")]
    txns = [
        Transaction(
            user_id=user.id,
            timestamp=base_time + timedelta(minutes=m),
            merchant="Test Merchant",
            category="groceries",
            amount=a,
            latitude=47.6062,
            longitude=-122.3321,
            location_label="Seattle, WA",
        )
        for m, a in zip(offsets, amounts, strict=True)
    ]
    db.add_all(txns)
    db.commit()
    user_id = user.id
    multi_hit_id = txns[-1].id
    db.close()

    response = client.get("/transactions", params={"user_id": user_id})
    assert response.status_code == 200
    items_by_id = {item["id"]: item for item in response.json()["items"]}

    multi_hit_rationale = items_by_id[multi_hit_id]["rationale"]
    assert "transactions" in multi_hit_rationale and "in 10 minutes" in multi_hit_rationale
    assert "higher than your typical spend" in multi_hit_rationale
    # RULES order (see engine.py), not hit-discovery order.
    assert items_by_id[multi_hit_id]["rule_names"] == ["velocity", "amount_deviation"]


def test_unflagged_transactions_have_null_rationale():
    db = TestingSessionLocal()
    user = User(name="No Flags User")
    db.add(user)
    db.flush()
    db.add_all(make_transaction(user.id, days_ago=i) for i in range(3))
    db.commit()
    user_id = user.id
    db.close()

    response = client.get("/transactions", params={"user_id": user_id})
    body = response.json()

    assert all(item["is_flagged"] is False for item in body["items"])
    assert all(item["rationale"] is None for item in body["items"])
    assert all(item["rule_names"] == [] for item in body["items"])


def test_flags_are_persisted_to_the_database():
    db = TestingSessionLocal()
    user = User(name="Persistence Check User")
    db.add(user)
    db.flush()
    history = [
        make_transaction(user.id, days_ago=10 - i, amount=Decimal("10.00")) for i in range(5)
    ]
    outlier = make_transaction(user.id, days_ago=0, amount=Decimal("500.00"))
    db.add_all([*history, outlier])
    db.commit()
    user_id = user.id
    outlier_id = outlier.id
    db.close()

    response = client.get("/transactions", params={"user_id": user_id})
    assert response.status_code == 200

    db = TestingSessionLocal()
    flags = db.query(TransactionFlag).filter(TransactionFlag.transaction_id == outlier_id).all()
    db.close()

    assert len(flags) == 1
    assert flags[0].rule_name == "amount_deviation"


def test_stale_flags_are_cleared_when_no_longer_triggered():
    db = TestingSessionLocal()
    user = User(name="Stale Flag User")
    db.add(user)
    db.flush()
    history = [
        make_transaction(user.id, days_ago=10 - i, amount=Decimal("10.00")) for i in range(5)
    ]
    outlier = make_transaction(user.id, days_ago=0, amount=Decimal("500.00"))
    db.add_all([*history, outlier])
    db.commit()
    user_id = user.id
    outlier_id = outlier.id
    db.close()

    first_response = client.get("/transactions", params={"user_id": user_id})
    first_items_by_id = {item["id"]: item for item in first_response.json()["items"]}
    assert first_items_by_id[outlier_id]["is_flagged"] is True

    # The outlier no longer deviates from its category's history.
    db = TestingSessionLocal()
    txn = db.query(Transaction).filter(Transaction.id == outlier_id).one()
    txn.amount = Decimal("10.00")
    db.commit()
    db.close()

    second_response = client.get("/transactions", params={"user_id": user_id})
    second_items_by_id = {item["id"]: item for item in second_response.json()["items"]}
    assert second_items_by_id[outlier_id]["is_flagged"] is False
    assert second_items_by_id[outlier_id]["rationale"] is None
    assert second_items_by_id[outlier_id]["rule_names"] == []

    db = TestingSessionLocal()
    flags = db.query(TransactionFlag).filter(TransactionFlag.transaction_id == outlier_id).all()
    db.close()
    assert flags == []


def test_concurrent_requests_dont_race_on_flag_refresh():
    """Regression test: React StrictMode double-fires the mount effect in
    dev, sending two near-simultaneous GET /transactions requests. Both used
    to run _refresh_flags's delete-then-reinsert concurrently, occasionally
    tripping the (transaction_id, rule_name) unique constraint with a 500.
    """
    db = TestingSessionLocal()
    user = User(name="Concurrent Refresh User")
    db.add(user)
    db.flush()
    history = [
        make_transaction(user.id, days_ago=10 - i, amount=Decimal("10.00")) for i in range(5)
    ]
    outlier = make_transaction(user.id, days_ago=0, amount=Decimal("500.00"))
    db.add_all([*history, outlier])
    db.commit()
    user_id = user.id
    outlier_id = outlier.id
    db.close()

    responses: list[object] = [None, None]

    def fetch(index: int) -> None:
        responses[index] = client.get("/transactions", params={"user_id": user_id})

    threads = [threading.Thread(target=fetch, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for response in responses:
        assert response.status_code == 200
        items_by_id = {item["id"]: item for item in response.json()["items"]}
        assert items_by_id[outlier_id]["is_flagged"] is True

    flags = db_query_flags_for(outlier_id)
    assert len(flags) == 1


def db_query_flags_for(transaction_id: int) -> list[TransactionFlag]:
    db = TestingSessionLocal()
    try:
        return db.query(TransactionFlag).filter(TransactionFlag.transaction_id == transaction_id).all()
    finally:
        db.close()
