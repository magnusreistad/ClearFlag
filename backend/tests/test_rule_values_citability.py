"""SCRUM-68's key cross-check: for each rule, the interim formatter's own
rationale string must pass validate_rationale() using ONLY a payload built
from that rule's RuleHit.values (app.investigation_agent.payload.
build_payload) plus the flagged transaction's own snapshot -- no tool
evidence required. This is what proves RuleHit.values actually grounds the
prose the rule already produces, not just that the keys exist.

Also covers the Meridian 843 three-rule case (tests/fixtures/
investigation_agent_meridian_843.json, regenerated for SCRUM-68 from a real
evaluate_all_rules() + investigation_graph.invoke() run) and confirms the
flagged transaction's own merchant/location_label are citable from the
transaction snapshot alone.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.investigation_agent.payload import build_payload
from app.investigation_agent.state import TransactionData
from app.investigation_agent.validation import validate_rationale
from app.models import Transaction, User
from app.rules.amount_deviation import evaluate_amount_deviation
from app.rules.engine import (
    evaluate_all_rules,
    rule_names_by_transaction,
    rule_values_by_transaction,
)
from app.rules.geographic_anomaly import evaluate_geographic_anomaly
from app.rules.new_merchant_risk import evaluate_new_merchant_risk
from app.rules.velocity import evaluate_velocity

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "investigation_agent_meridian_843.json"

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def db_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def new_user(db: Session) -> int:
    user = User(name="Citability Test User")
    db.add(user)
    db.flush()
    return user.id


def _transaction_data(t: Transaction) -> TransactionData:
    return {
        "id": t.id,
        "user_id": t.user_id,
        "timestamp": t.timestamp,
        "merchant": t.merchant,
        "category": t.category,
        "amount": t.amount,
        "latitude": t.latitude,
        "longitude": t.longitude,
        "location_label": t.location_label,
    }


def _assert_hit_grounds_itself(transaction: Transaction, rule_name: str, hit) -> None:
    """The core SCRUM-68 cross-check: hit.rationale must validate against a
    payload built purely from hit.values and the transaction snapshot --
    evidence={} (no tool call in this run at all)."""
    payload = build_payload(_transaction_data(transaction), [rule_name], {rule_name: hit.values})

    result = validate_rationale(hit.rationale, payload, evidence={})

    assert result.passed is True, result.violations


class TestVelocityGroundsItself:
    def test_burst_rationale_grounds_against_its_own_values(self):
        db = TestingSessionLocal()
        user_id = new_user(db)
        txns = [
            Transaction(
                user_id=user_id,
                timestamp=BASE_TIME + timedelta(minutes=m),
                merchant="Test Merchant",
                category="groceries",
                amount=Decimal("10.00"),
                latitude=47.6062,
                longitude=-122.3321,
                location_label="Seattle, WA",
            )
            for m in [0, 2, 4, 6, 8, 10]
        ]
        db.add_all(txns)
        db.commit()
        db.close()

        hits = evaluate_velocity(txns, max_count=5, window_minutes=10)

        assert len(hits) == 6
        _assert_hit_grounds_itself(txns[0], "velocity", hits[0])


class TestAmountDeviationGroundsItself:
    def test_outlier_rationale_grounds_against_its_own_values(self):
        db = TestingSessionLocal()
        user_id = new_user(db)
        history_amounts = [Decimal("60.00"), Decimal("55.00"), Decimal("70.00"), Decimal("65.00"), Decimal("58.00")]
        txns = [
            Transaction(
                user_id=user_id,
                timestamp=BASE_TIME + timedelta(hours=i),
                merchant="Grocer",
                category="groceries",
                amount=amount,
                latitude=47.6062,
                longitude=-122.3321,
                location_label="Seattle, WA",
            )
            for i, amount in enumerate(history_amounts)
        ]
        outlier = Transaction(
            user_id=user_id,
            timestamp=BASE_TIME + timedelta(hours=5),
            merchant="Grocer",
            category="groceries",
            amount=Decimal("540.00"),
            latitude=47.6062,
            longitude=-122.3321,
            location_label="Seattle, WA",
        )
        db.add_all([*txns, outlier])
        db.commit()
        db.close()

        hits = evaluate_amount_deviation([*txns, outlier])

        assert len(hits) == 1
        _assert_hit_grounds_itself(outlier, "amount_deviation", hits[0])


class TestNewMerchantRiskGroundsItself:
    def test_outlier_first_purchase_rationale_grounds_against_its_own_values(self):
        db = TestingSessionLocal()
        user_id = new_user(db)
        history_amounts = [Decimal("60.00"), Decimal("55.00"), Decimal("70.00"), Decimal("65.00"), Decimal("58.00")]
        txns = [
            Transaction(
                user_id=user_id,
                timestamp=BASE_TIME + timedelta(days=i),
                merchant=f"Merchant {i}",
                category="shopping",
                amount=amount,
                latitude=47.6062,
                longitude=-122.3321,
                location_label="Seattle, WA",
            )
            for i, amount in enumerate(history_amounts)
        ]
        outlier = Transaction(
            user_id=user_id,
            timestamp=BASE_TIME + timedelta(days=5),
            merchant="New Boutique",
            category="shopping",
            amount=Decimal("540.00"),
            latitude=47.6062,
            longitude=-122.3321,
            location_label="Seattle, WA",
        )
        db.add_all([*txns, outlier])
        db.commit()
        db.close()

        hits = evaluate_new_merchant_risk([*txns, outlier])

        assert len(hits) == 1
        _assert_hit_grounds_itself(outlier, "new_merchant_risk", hits[0])


class TestGeographicAnomalyGroundsItself:
    def test_far_transaction_rationale_grounds_against_its_own_values(self):
        db = TestingSessionLocal()
        user_id = new_user(db)
        metro = [
            (47.6062, -122.3321, "Seattle, WA"),
            (47.6101, -122.2015, "Bellevue, WA"),
            (47.2529, -122.4443, "Tacoma, WA"),
            (47.9790, -122.2021, "Everett, WA"),
            (47.6740, -122.1215, "Redmond, WA"),
        ]
        history = [
            Transaction(
                user_id=user_id,
                timestamp=BASE_TIME + timedelta(days=i),
                merchant="Test Merchant",
                category="shopping",
                amount=Decimal("10.00"),
                latitude=lat,
                longitude=lon,
                location_label=label,
            )
            for i, (lat, lon, label) in enumerate(metro)
        ]
        candidate = Transaction(
            user_id=user_id,
            timestamp=BASE_TIME + timedelta(days=len(metro)),
            merchant="Test Merchant",
            category="shopping",
            amount=Decimal("10.00"),
            latitude=45.5152,
            longitude=-122.6784,
            location_label="Portland, OR",
        )
        db.add_all([*history, candidate])
        db.commit()
        db.close()

        hits = evaluate_geographic_anomaly([*history, candidate])

        assert len(hits) == 1
        _assert_hit_grounds_itself(candidate, "geographic_anomaly", hits[0])


class TestMeridian843ThreeRuleCase:
    """The regenerated fixture's payload already combines all three rules'
    RuleHit.values plus the transaction 843 snapshot -- built by
    build_payload from a real evaluate_all_rules() run (see
    scripts/ used to capture it, and the fixture's own _source note).
    """

    @pytest.fixture
    def payload(self):
        raw = json.loads(FIXTURE_PATH.read_text())
        payload = raw["payload"]
        payload["transaction"]["amount"] = Decimal(payload["transaction"]["amount"])
        for key in ("amount", "category_mean", "category_stdev", "percent_above_mean"):
            payload["rules"]["amount_deviation"][key] = Decimal(payload["rules"]["amount_deviation"][key])
        for key in ("amount", "typical_first_purchase_mean", "typical_first_purchase_stdev", "percent_above_mean"):
            payload["rules"]["new_merchant_risk"][key] = Decimal(payload["rules"]["new_merchant_risk"][key])
        return payload

    # Captured verbatim from the same evaluate_all_rules() run that produced
    # the fixture (transaction 843, user_id 2) -- see FIXTURE_PATH's
    # _source note.
    RATIONALE_BY_RULE: ClassVar[dict[str, str]] = {
        "geographic_anomaly": (
            "Flagged: This transaction occurred in Manila, Philippines, far from where you usually shop."
        ),
        "amount_deviation": "Flagged: This amount is 1873% higher than your typical spend in this category.",
        "new_merchant_risk": (
            "Flagged: This is your first purchase from this merchant, and the amount is "
            "2867% higher than your typical first-time purchase."
        ),
    }

    @pytest.mark.parametrize("rule_name", ["geographic_anomaly", "amount_deviation", "new_merchant_risk"])
    def test_each_rules_own_rationale_grounds_against_the_shared_payload(self, payload, rule_name):
        rationale = self.RATIONALE_BY_RULE[rule_name]

        result = validate_rationale(rationale, payload, evidence={})

        assert result.passed is True, result.violations

    def test_concatenated_three_rule_rationale_grounds_against_the_shared_payload(self, payload):
        concatenated = " ".join(self.RATIONALE_BY_RULE[name] for name in payload["rule_names"])

        result = validate_rationale(concatenated, payload, evidence={})

        assert result.passed is True, result.violations


class TestTransactionSnapshotCitability:
    """Confirms the flagged transaction's own attributes -- not just rule
    values -- are citable from the payload's transaction snapshot alone."""

    def test_merchant_and_location_label_are_citable_with_no_rule_values(self):
        transaction_data: TransactionData = {
            "id": 843,
            "user_id": 2,
            "timestamp": BASE_TIME,
            "merchant": "Meridian Duty-Free Traders",
            "category": "shopping",
            "amount": Decimal("3200.00"),
            "latitude": 14.5995,
            "longitude": 120.9842,
            "location_label": "Manila, Philippines",
        }
        payload = build_payload(transaction_data, [], {})
        rationale = (
            "Your purchase from Meridian Duty-Free Traders was flagged -- it "
            "occurred in Manila, Philippines."
        )

        result = validate_rationale(rationale, payload, evidence={})

        assert result.passed is True, result.violations


def test_rule_values_by_transaction_feeds_build_payload_end_to_end():
    """Integration check that app.rules.engine.rule_values_by_transaction's
    output is exactly the shape build_payload expects for `rule_values`."""
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = [
        Transaction(
            user_id=user_id,
            timestamp=BASE_TIME + timedelta(hours=i),
            merchant="Test Merchant",
            category="groceries",
            amount=Decimal("10.00"),
            latitude=47.6062,
            longitude=-122.3321,
            location_label="Seattle, WA",
        )
        for i in range(5)
    ]
    outlier = Transaction(
        user_id=user_id,
        timestamp=BASE_TIME + timedelta(hours=5),
        merchant="Test Merchant",
        category="groceries",
        amount=Decimal("500.00"),
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )
    db.add_all([*txns, outlier])
    db.commit()
    db.close()

    hits = evaluate_all_rules([*txns, outlier])
    rule_names = rule_names_by_transaction(hits)[outlier.id]
    rule_values = rule_values_by_transaction(hits)[outlier.id]

    payload = build_payload(_transaction_data(outlier), rule_names, rule_values)

    assert payload["rule_names"] == ["amount_deviation"]
    assert payload["rules"] == {"amount_deviation": hits[0].values}
