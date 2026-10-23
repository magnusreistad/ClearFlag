import sys
from datetime import UTC, datetime
from decimal import Decimal

from app.database import SessionLocal
from app.models import AgentRationale, Transaction, User
from app.rules import new_merchant_risk
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


def flagged_by(rule_name: str) -> list[Transaction]:
    """The seed's transactions that `rule_name` flags. The per-rule guards
    below compare these to an exact set, so a plant that stops flagging and
    a baseline row that starts both fail."""
    transactions = seed_dataset()
    by_id = {t.id: t for t in transactions}
    return [
        by_id[h.transaction_id]
        for h in evaluate_all_rules(transactions)
        if h.rule_name == rule_name
    ]


def test_seed_velocity_flags_exactly_each_bursts_sixth_charge():
    """SCRUM-89: the planted bursts used to be spaced i*randint(3,8) minutes
    apart, which never put more than 5 transactions in one 10-minute window,
    so the demo had no velocity flag at all."""
    assert sorted(t.merchant for t in flagged_by("velocity")) == [
        "QuickPay Test Merchant 1-6",
        "QuickPay Test Merchant 2-6",
        "QuickPay Test Merchant 3-6",
    ]


def test_seed_geographic_anomaly_flags_exactly_the_far_away_plants():
    """The three geographic plants plus SCRUM-65's Meridian (Manila); their
    merchants are ordinary baseline ones, so they're told apart by place."""
    assert sorted(t.location_label for t in flagged_by("geographic_anomaly")) == [
        "Bangkok, Thailand",
        "Lagos, Nigeria",
        "Manila, Philippines",
        "Moscow, Russia",
    ]


def test_seed_amount_deviation_flags_exactly_the_large_plants():
    """The four amount plants (at merchants the baseline also uses, so keyed
    by amount too) and every new-merchant, multi- and triple-rule plant."""
    assert sorted((t.merchant, t.amount) for t in flagged_by("amount_deviation")) == [
        ("AMC Theatres", Decimal("420.00")),
        ("Aurora Fine Jewelers", Decimal("2150.00")),
        ("CVS Pharmacy", Decimal("380.00")),
        ("Chipotle Mexican Grill", Decimal("310.00")),
        ("Meridian Duty-Free Traders", Decimal("3200.00")),
        ("Nordic Home Furnishings", Decimal("780.00")),
        ("Precision Optics Co.", Decimal("610.00")),
        ("Riverside Wellness Spa", Decimal("340.00")),
        ("Whole Foods Market", Decimal("540.00")),
    ]


def test_seed_new_merchant_risk_flags_exactly_the_new_merchant_plants():
    """SCRUM-90: Nordic Home Furnishings never flagged on its old day 91,
    after Aurora's $2150 first purchase had inflated its baseline."""
    assert sorted(t.merchant for t in flagged_by("new_merchant_risk")) == [
        "Aurora Fine Jewelers",
        "Meridian Duty-Free Traders",
        "Nordic Home Furnishings",
        "Precision Optics Co.",
        "Riverside Wellness Spa",
    ]


def test_seed_nordic_clears_the_new_merchant_threshold_with_margin():
    """SCRUM-90: a plant that barely clears (SCRUM-89's velocity bursts) stops
    flagging on the next unrelated seed change. Nordic's z-score is ~7.04;
    hold it to at least twice the rule's threshold."""
    transactions = seed_dataset()
    [nordic] = [t for t in transactions if t.merchant == "Nordic Home Furnishings"]
    [values] = [
        h.values
        for h in evaluate_all_rules(transactions)
        if h.rule_name == "new_merchant_risk" and h.transaction_id == nordic.id
    ]
    z_score = (values["amount"] - values["typical_first_purchase_mean"]) / values[
        "typical_first_purchase_stdev"
    ]
    assert z_score >= 2 * new_merchant_risk.DEFAULT_STD_DEV_THRESHOLD


def test_seed_flag_totals():
    """The seed's demo numbers (SCRUM-90): 15 flagged transactions, 21 rule
    hits -- velocity 3, geographic anomaly 4, amount deviation 9, new
    merchant 5."""
    hits = evaluate_all_rules(seed_dataset())
    assert len({h.transaction_id for h in hits}) == 15
    assert len(hits) == 21


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
