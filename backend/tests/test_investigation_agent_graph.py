from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.investigation_agent import graph as graph_module
from app.investigation_agent.graph import get_transaction_history, investigation_graph
from app.investigation_agent.state import InvestigationState, TransactionData
from app.models import Transaction, User
from scripts.seed_transactions import generate_triple_rule_fraud

# Same in-memory-SQLite-per-test pattern as tests/test_transactions.py, applied
# here to the Investigation Agent's own SessionLocal (app.database.SessionLocal,
# imported into graph.py at module scope) since get_transaction_history opens
# its own session rather than taking one as a FastAPI-injected dependency --
# there's no request-scoped session to override yet (that's SCRUM-53).
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def db_schema(monkeypatch):
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(graph_module, "SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=engine)


def _transaction_data(transaction, *, id_: int, user_id: int) -> TransactionData:
    return {
        "id": id_,
        "user_id": user_id,
        "timestamp": transaction.timestamp,
        "merchant": transaction.merchant,
        "category": transaction.category,
        "amount": transaction.amount,
        "latitude": transaction.latitude,
        "longitude": transaction.longitude,
        "location_label": transaction.location_label,
    }


def _make_transaction(
    user_id: int,
    timestamp: datetime,
    *,
    merchant: str = "Test Merchant",
    amount: Decimal = Decimal("10.00"),
) -> Transaction:
    return Transaction(
        user_id=user_id,
        timestamp=timestamp,
        merchant=merchant,
        category="groceries",
        amount=amount,
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )


def test_scrum_65_triple_rule_transaction_fans_out_to_its_two_mapped_tool_branches():
    """SCRUM-65's seed transaction (Meridian Duty-Free Traders, Manila) trips
    new_merchant_risk, amount_deviation, and geographic_anomaly -- velocity
    is deliberately excluded from that plant. Of the three triggered rules,
    only new_merchant_risk and geographic_anomaly map to a tool node
    (SCRUM-51: amount_deviation needs no tool -- its values already come
    from the rules engine's invocation payload). So this transaction should
    visit exactly get_merchant_risk_score and get_geo_distance.
    """
    start_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    [(seed_transaction, _)] = generate_triple_rule_fraud(user_id=1, start_date=start_date)

    state: InvestigationState = {
        "transaction": _transaction_data(seed_transaction, id_=999, user_id=1),
        "rule_names": ["new_merchant_risk", "amount_deviation", "geographic_anomaly"],
        "evidence": {},
        "rationale": "",
    }

    result = investigation_graph.invoke(state)

    assert set(result["evidence"].keys()) == {"get_merchant_risk_score", "get_geo_distance"}
    assert result["evidence"]["get_merchant_risk_score"]["merchant"] == "Meridian Duty-Free Traders"
    assert result["evidence"]["get_geo_distance"]["latitude"] == seed_transaction.latitude
    assert result["rationale"] == ""  # assemble_rationale is a no-op placeholder until SCRUM-53


def test_velocity_only_flag_visits_get_transaction_history_branch():
    """Covers the one tool branch the SCRUM-65 transaction can never reach
    (get_transaction_history, mapped from velocity): that seed transaction
    deliberately excludes velocity, since it requires a 6+ transaction
    burst that doesn't compose with a single planted transaction. A
    separate synthetic single-rule case exercises this branch instead.
    """
    anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state: InvestigationState = {
        "transaction": {
            "id": 1,
            "user_id": 1,
            "timestamp": anchor,
            "merchant": "Test Merchant",
            "category": "groceries",
            "amount": Decimal("10.00"),
            "latitude": 47.6062,
            "longitude": -122.3321,
            "location_label": "Seattle, WA",
        },
        "rule_names": ["velocity"],
        "evidence": {},
        "rationale": "",
    }

    result = investigation_graph.invoke(state)

    assert set(result["evidence"].keys()) == {"get_transaction_history"}


def test_amount_deviation_only_flag_has_no_tool_and_routes_straight_to_assemble():
    """amount_deviation maps to no tool (SCRUM-51) -- its evidence already
    comes from the rules engine, not a tool call, so a flag triggered by
    only that rule should reach assemble_rationale with no evidence
    gathered rather than being routed nowhere.
    """
    state: InvestigationState = {
        "transaction": {
            "id": 2,
            "user_id": 1,
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "merchant": "Test Merchant",
            "category": "shopping",
            "amount": Decimal("500.00"),
            "latitude": 47.6062,
            "longitude": -122.3321,
            "location_label": "Seattle, WA",
        },
        "rule_names": ["amount_deviation"],
        "evidence": {},
        "rationale": "",
    }

    result = investigation_graph.invoke(state)

    assert result["evidence"] == {}


class TestGetTransactionHistory:
    """SCRUM-48: direct unit tests of the get_transaction_history node
    against a real (in-memory SQLite) DB session, independent of the graph's
    routing -- the routing tests above already cover that this node gets
    reached for a velocity-only flag.
    """

    def _seed_user_and_anchor(self, db, *, anchor: datetime) -> tuple[User, Transaction]:
        user = User(name="Test User")
        db.add(user)
        db.flush()
        anchor_transaction = _make_transaction(user.id, anchor, merchant="Anchor Merchant", amount=Decimal("25.00"))
        db.add(anchor_transaction)
        db.flush()
        return user, anchor_transaction

    def test_transactions_inside_window_are_returned_with_correct_count(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user, anchor_transaction = self._seed_user_and_anchor(db, anchor=anchor)
            # 4 minutes before the anchor, inside the 10-minute window.
            inside = _make_transaction(
                user.id, anchor - timedelta(minutes=4), merchant="Corner Store", amount=Decimal("5.00")
            )
            # Exactly at the window boundary (diff == window_minutes) -- velocity.py's
            # own `> window` exclusion test keeps this one in, so this tool should too.
            boundary = _make_transaction(
                user.id, anchor - timedelta(minutes=10), merchant="Boundary Shop", amount=Decimal("7.50")
            )
            db.add_all([inside, boundary])
            db.commit()

            state: InvestigationState = {
                "transaction": _transaction_data(anchor_transaction, id_=anchor_transaction.id, user_id=user.id),
                "rule_names": ["velocity"],
                "evidence": {},
                "rationale": "",
            }
            result = get_transaction_history(state)
        finally:
            db.close()

        evidence = result["evidence"]["get_transaction_history"]
        assert evidence["user_id"] == user.id
        assert evidence["count"] == 3
        merchants = {t["merchant"] for t in evidence["transactions"]}
        assert merchants == {"Anchor Merchant", "Corner Store", "Boundary Shop"}

    def test_transactions_outside_window_are_excluded(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user, anchor_transaction = self._seed_user_and_anchor(db, anchor=anchor)
            # 11 minutes before the anchor -- just outside the 10-minute window.
            outside = _make_transaction(
                user.id, anchor - timedelta(minutes=11), merchant="Too Early Shop", amount=Decimal("15.00")
            )
            # After the anchor -- the window only looks backward from it.
            after = _make_transaction(
                user.id, anchor + timedelta(minutes=1), merchant="Too Late Shop", amount=Decimal("12.00")
            )
            db.add_all([outside, after])
            db.commit()

            state: InvestigationState = {
                "transaction": _transaction_data(anchor_transaction, id_=anchor_transaction.id, user_id=user.id),
                "rule_names": ["velocity"],
                "evidence": {},
                "rationale": "",
            }
            result = get_transaction_history(state)
        finally:
            db.close()

        evidence = result["evidence"]["get_transaction_history"]
        assert evidence["count"] == 1
        assert [t["merchant"] for t in evidence["transactions"]] == ["Anchor Merchant"]

    def test_user_with_no_matching_transactions_returns_empty_history(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            other_user = User(name="Other User")
            db.add(other_user)
            db.flush()
            # Belongs to a different user -- must not leak into this user's history.
            db.add(_make_transaction(other_user.id, anchor, merchant="Someone Else's Purchase"))
            db.commit()

            state: InvestigationState = {
                "transaction": {
                    "id": 12345,
                    "user_id": 999,
                    "timestamp": anchor,
                    "merchant": "Test Merchant",
                    "category": "groceries",
                    "amount": Decimal("10.00"),
                    "latitude": 47.6062,
                    "longitude": -122.3321,
                    "location_label": "Seattle, WA",
                },
                "rule_names": ["velocity"],
                "evidence": {},
                "rationale": "",
            }
            result = get_transaction_history(state)
        finally:
            db.close()

        evidence = result["evidence"]["get_transaction_history"]
        assert evidence["transactions"] == []
        assert evidence["count"] == 0
