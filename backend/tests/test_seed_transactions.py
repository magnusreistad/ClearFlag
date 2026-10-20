import sys
from datetime import UTC, datetime
from decimal import Decimal

from app.database import SessionLocal
from app.models import AgentRationale, Transaction, User
from app.rules.engine import evaluate_all_rules, rule_names_by_transaction
from scripts import seed_transactions
from scripts.seed_transactions import SEED_USER_NAME, generate_dataset

START_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def seed_dataset() -> list[Transaction]:
    """Everything the seed inserts, in insertion order, with ids assigned the
    way a fresh database would -- generate_dataset never touches one."""
    baseline, fraud_groups = generate_dataset(user_id=1, start_date=START_DATE)
    transactions = baseline + [tx for _, rows in fraud_groups for tx, _ in rows]
    for transaction_id, transaction in enumerate(transactions, start=1):
        transaction.id = transaction_id
    return transactions


def test_seed_produces_at_least_one_velocity_flag():
    """SCRUM-89: the planted bursts used to be spaced i*randint(3,8) minutes
    apart, which never put more than 5 transactions in one 10-minute window,
    so the demo had no velocity flag at all."""
    hits = [h for h in evaluate_all_rules(seed_dataset()) if h.rule_name == "velocity"]
    assert hits, "seed no longer trips velocity -- check generate_velocity_fraud"


def test_seed_triple_rule_plant_still_trips_its_three_rules():
    """SCRUM-65's Meridian plant, over the full seed rather than in isolation
    -- every other plant shares its baselines, so a change elsewhere in the
    seed can quietly stop it flagging."""
    transactions = seed_dataset()
    rule_names = rule_names_by_transaction(evaluate_all_rules(transactions))
    [meridian] = [t for t in transactions if t.merchant == "Meridian Duty-Free Traders"]
    assert rule_names[meridian.id] == [
        "geographic_anomaly",
        "amount_deviation",
        "new_merchant_risk",
    ]


def run_seed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["seed_transactions", "--yes"])
    seed_transactions.main()


def add_rationale(db, transaction_id: int) -> None:
    db.add(
        AgentRationale(
            transaction_id=transaction_id,
            fact_fingerprint="fingerprint",
            prompt_version="v1",
            model_id="mock",
            validation_passed=False,
            violations=[],
        )
    )


def test_reseed_succeeds_when_the_user_has_agent_rationales(monkeypatch):
    """SCRUM-89: agent_rationales.transaction_id has no ON DELETE CASCADE, so
    a reseed used to fail with a ForeignKeyViolation once compose_rationales
    had written any row (pass or fail) for the seeded user. Another user's
    rationales must survive it."""
    run_seed(monkeypatch)

    db = SessionLocal()
    seeded = db.query(Transaction).join(User).filter(User.name == SEED_USER_NAME)
    add_rationale(db, seeded.first().id)
    other_user = User(name="Other User")
    db.add(other_user)
    db.flush()
    other_transaction = Transaction(
        user_id=other_user.id,
        timestamp=START_DATE,
        merchant="Other Merchant",
        category="groceries",
        amount=Decimal("10.00"),
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )
    db.add(other_transaction)
    db.flush()
    other_user_id, other_transaction_id = other_user.id, other_transaction.id
    add_rationale(db, other_transaction_id)
    db.commit()
    db.close()

    run_seed(monkeypatch)

    db = SessionLocal()
    remaining = [r.transaction_id for r in db.query(AgentRationale).all()]
    assert remaining == [other_transaction_id]
    assert (
        db.query(Transaction).filter(Transaction.user_id == other_user_id).count() == 1
    )
    db.close()
