from datetime import UTC, datetime

from app.models import Transaction
from app.rules.engine import evaluate_all_rules, rule_names_by_transaction
from scripts.seed_transactions import generate_dataset

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
