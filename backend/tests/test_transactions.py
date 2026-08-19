from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Transaction, User

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


def make_transaction(user_id: int, days_ago: int, merchant: str = "Test Merchant") -> Transaction:
    return Transaction(
        user_id=user_id,
        timestamp=datetime.now(timezone.utc) - timedelta(days=days_ago),
        merchant=merchant,
        category="groceries",
        amount=Decimal("10.00"),
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
