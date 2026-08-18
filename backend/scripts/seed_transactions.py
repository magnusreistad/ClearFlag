"""Generate a synthetic transaction dataset for the seeded ClearFlag user.

Produces ~95 days of realistic, mostly-unremarkable spending for the single
seeded user, with a handful of deliberately planted fraud scenarios mixed in
(see PLANTED FRAUD SCENARIOS below). The Sprint 2 rules engine will consume
this data; this script only needs the planted rows to be *distinct enough*
from the baseline to be caught by reasonable thresholds later — exact
thresholds are not decided yet.

Idempotent: re-running clears this user's existing transactions first, so
you always end up with exactly one clean generated dataset, not duplicates.
Uses a fixed random seed, so the dataset is reproducible.

Only ever run this against the Neon `dev` branch — see the DATABASE_URL
confirmation prompt below. Never run it against `production`.

Run with: python -m scripts.seed_transactions
Skip the confirmation prompt (e.g. in automation): python -m scripts.seed_transactions --yes
"""

import argparse
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database import DATABASE_URL, SessionLocal
from app.models import Transaction, User

SEED = 42
DAYS_OF_HISTORY = 95  # comfortably over the 90-day acceptance criterion

# Home location cluster: small jitter around one city to simulate everyday
# spending near home/work. Planted geographic-anomaly transactions (below)
# are placed far outside this cluster on purpose.
HOME_CITY = {"label": "Seattle, WA", "lat": 47.6062, "lon": -122.3321}
HOME_JITTER_DEGREES = 0.05  # roughly a several-mile radius around HOME_CITY

FAR_LOCATIONS = [
    {"label": "Bangkok, Thailand", "lat": 13.7563, "lon": 100.5018},
    {"label": "Lagos, Nigeria", "lat": 6.5244, "lon": 3.3792},
    {"label": "Moscow, Russia", "lat": 55.7558, "lon": 37.6173},
    {"label": "Manila, Philippines", "lat": 14.5995, "lon": 120.9842},
]

# Baseline spending categories: merchant pools (weighted so a couple of
# merchants per category are "regulars") plus a per-category amount
# distribution (mean, stdev, floor) used to draw normal transaction amounts.
CATEGORIES = {
    "groceries": {
        "merchants": [("Whole Foods Market", 4), ("Trader Joe's", 3), ("Safeway", 2), ("QFC", 1)],
        "mean": 62, "stdev": 22, "floor": 8,
    },
    "dining": {
        "merchants": [("Starbucks", 5), ("Chipotle Mexican Grill", 3), ("Panera Bread", 2), ("Din Tai Fung", 1)],
        "mean": 21, "stdev": 12, "floor": 4,
    },
    "gas_transport": {
        "merchants": [("Shell", 3), ("Chevron", 3), ("Uber", 2), ("Lyft", 1)],
        "mean": 42, "stdev": 14, "floor": 12,
    },
    "shopping": {
        "merchants": [("Amazon", 5), ("Target", 3), ("Best Buy", 1), ("REI", 1)],
        "mean": 58, "stdev": 32, "floor": 10,
    },
    "entertainment": {
        "merchants": [("AMC Theatres", 3), ("Steam", 2), ("Ticketmaster", 1)],
        "mean": 34, "stdev": 18, "floor": 8,
    },
    "health": {
        "merchants": [("CVS Pharmacy", 3), ("Walgreens", 2), ("Bartell Drugs", 1)],
        "mean": 28, "stdev": 16, "floor": 6,
    },
}
CATEGORY_WEIGHTS = {
    "groceries": 22, "dining": 26, "gas_transport": 16,
    "shopping": 14, "entertainment": 12, "health": 10,
}

# Recurring monthly charges: a realistic touch that's also useful baseline
# noise around the planted "new merchant" scenarios below.
SUBSCRIPTIONS = [
    ("Netflix", Decimal("15.99")), ("Spotify", Decimal("10.99")),
    ("Disney+", Decimal("7.99")), ("Amazon Prime", Decimal("14.99")),
]
UTILITIES = [
    ("Seattle City Light", 70, 140), ("Comcast Xfinity", 60, 110),
    ("Puget Sound Energy", 50, 130),
]

# Hours weighted toward typical waking/spending hours; near-zero overnight.
HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 4, 6, 8, 8, 8, 9, 9, 8, 8, 8, 9, 10, 9, 8, 6, 4, 2, 1]


def confirm_dev_database(skip_prompt: bool) -> None:
    """Refuse to run without an explicit human confirmation of the target DB.

    This project has no way to tell a `dev` Neon branch from `production`
    purely by inspecting the connection string, so this is a human checkpoint
    rather than an automated one: it prints exactly what DATABASE_URL points
    at and requires the operator to confirm before any data is touched.
    """
    host = DATABASE_URL.split("@")[-1].split("/")[0] if DATABASE_URL else "(unset)"
    print(f"Target database host: {host}")
    if skip_prompt:
        return
    reply = input("Confirm this is the `dev` Neon branch, not `production` [y/N]: ").strip().lower()
    if reply != "y":
        raise SystemExit("Aborted: confirmation not given.")


SEED_USER_NAME = "Demo User"


def get_or_create_seed_user(db) -> User:
    user = db.query(User).filter(User.name == SEED_USER_NAME).first()
    if user is None:
        user = User(name=SEED_USER_NAME)
        db.add(user)
        db.flush()
    return user


def jittered_home_location() -> tuple[float, float, str]:
    lat = HOME_CITY["lat"] + random.uniform(-HOME_JITTER_DEGREES, HOME_JITTER_DEGREES)
    lon = HOME_CITY["lon"] + random.uniform(-HOME_JITTER_DEGREES, HOME_JITTER_DEGREES)
    return lat, lon, HOME_CITY["label"]


def random_timestamp(day: datetime) -> datetime:
    hour = random.choices(range(24), weights=HOUR_WEIGHTS)[0]
    return day.replace(hour=hour, minute=random.randint(0, 59), second=random.randint(0, 59))


def draw_amount(mean: float, stdev: float, floor: float) -> Decimal:
    value = max(floor, random.gauss(mean, stdev))
    return Decimal(str(round(value, 2)))


def make_transaction(user_id: int, timestamp: datetime, merchant: str, category: str, amount: Decimal,
                      location: tuple[float, float, str]) -> Transaction:
    lat, lon, label = location
    return Transaction(
        user_id=user_id, timestamp=timestamp, merchant=merchant, category=category,
        amount=amount, latitude=lat, longitude=lon, location_label=label,
    )


def generate_baseline(user_id: int, start_date: datetime) -> list[Transaction]:
    """Everyday, unremarkable spending across all categories.

    Each day's transactions independently draw a random hour/minute, so on a
    high-volume day (up to 3 tx) there's a small chance two baseline rows land
    close together purely by chance. That's realistic noise, not a bug — but
    worth knowing about if Sprint 2 threshold-tuning ever sees a stray
    near-velocity signal outside the planted clusters below.
    """
    transactions = []
    categories = list(CATEGORY_WEIGHTS.keys())
    weights = list(CATEGORY_WEIGHTS.values())

    for day_offset in range(DAYS_OF_HISTORY):
        day = start_date + timedelta(days=day_offset)
        num_tx = random.choices([0, 1, 2, 3], weights=[10, 40, 35, 15])[0]
        for _ in range(num_tx):
            category = random.choices(categories, weights=weights)[0]
            spec = CATEGORIES[category]
            merchant = random.choices(
                [m for m, _ in spec["merchants"]], weights=[w for _, w in spec["merchants"]]
            )[0]
            amount = draw_amount(spec["mean"], spec["stdev"], spec["floor"])
            transactions.append(make_transaction(
                user_id, random_timestamp(day), merchant, category, amount, jittered_home_location(),
            ))

    # Recurring monthly subscriptions and utilities, once per ~30-day window.
    for month in range(DAYS_OF_HISTORY // 30 + 1):
        day = start_date + timedelta(days=min(month * 30 + random.randint(0, 3), DAYS_OF_HISTORY - 1))
        for merchant, amount in SUBSCRIPTIONS:
            transactions.append(make_transaction(
                user_id, random_timestamp(day), merchant, "subscriptions", amount, jittered_home_location(),
            ))
        for merchant, low, high in UTILITIES:
            amount = Decimal(str(round(random.uniform(low, high), 2)))
            transactions.append(make_transaction(
                user_id, random_timestamp(day), merchant, "utilities", amount, jittered_home_location(),
            ))

    return transactions


# --- PLANTED FRAUD SCENARIOS -------------------------------------------
#
# The four generators below insert rows that are deliberately implausible
# against the baseline distributions above, so they'd be caught by
# reasonable Sprint 2 rules-engine thresholds even though those thresholds
# aren't finalized yet. Each generator returns (Transaction, reason) pairs
# so main() can print a verification summary after inserting.

def generate_velocity_fraud(user_id: int, start_date: datetime) -> list[tuple[Transaction, str]]:
    """Rule 1 — velocity: clusters of several transactions in a short window.

    Simulates card-testing: a burst of small purchases at different
    merchants within under an hour, on days otherwise unrelated to each
    other. Three separate clusters, planted on different days.
    """
    results = []
    burst_days = [12, 41, 77]
    for cluster_index, day_offset in enumerate(burst_days):
        day = start_date + timedelta(days=day_offset)
        cluster_start = random_timestamp(day)
        for i in range(6):
            ts = cluster_start + timedelta(minutes=i * random.randint(3, 8))
            category = random.choice(["shopping", "gas_transport", "entertainment"])
            merchant = f"QuickPay Test Merchant {cluster_index + 1}-{i + 1}"
            amount = Decimal(str(round(random.uniform(4, 30), 2)))
            tx = make_transaction(user_id, ts, merchant, category, amount, jittered_home_location())
            results.append((tx, f"velocity cluster #{cluster_index + 1}: 6 charges within ~30 min"))
    return results


def generate_geo_anomaly_fraud(user_id: int, start_date: datetime) -> list[tuple[Transaction, str]]:
    """Rule 2 — geographic anomaly: a transaction far from the home cluster.

    Amount looks ordinary for its category; only the location is wrong,
    since that's the realistic signature of a compromised card being used
    far from where the cardholder actually is.
    """
    results = []
    plant_days = [8, 33, 60]
    for day_offset, far in zip(plant_days, FAR_LOCATIONS):
        day = start_date + timedelta(days=day_offset)
        category = random.choice(["dining", "shopping", "groceries"])
        spec = CATEGORIES[category]
        amount = draw_amount(spec["mean"], spec["stdev"], spec["floor"])
        merchant = random.choices(
            [m for m, _ in spec["merchants"]], weights=[w for _, w in spec["merchants"]]
        )[0]
        location = (far["lat"], far["lon"], far["label"])
        tx = make_transaction(user_id, random_timestamp(day), merchant, category, amount, location)
        results.append((tx, f"geographic anomaly: {far['label']}, far outside the home cluster"))
    return results


def generate_amount_deviation_fraud(user_id: int, start_date: datetime) -> list[tuple[Transaction, str]]:
    """Rule 3 — amount deviation: far outside the user's typical spend for
    that merchant category, at an otherwise-regular merchant/location.
    """
    results = []
    plants = [
        (18, "groceries", "Whole Foods Market", Decimal("540.00")),   # normal mean ~62
        (49, "dining", "Chipotle Mexican Grill", Decimal("310.00")),   # normal mean ~21
        (70, "entertainment", "AMC Theatres", Decimal("420.00")),      # normal mean ~34
        (85, "health", "CVS Pharmacy", Decimal("380.00")),             # normal mean ~28
    ]
    for day_offset, category, merchant, amount in plants:
        day = start_date + timedelta(days=day_offset)
        tx = make_transaction(user_id, random_timestamp(day), merchant, category, amount, jittered_home_location())
        results.append((tx, f"amount deviation: {amount} vs category mean ~{CATEGORIES[category]['mean']}"))
    return results


def generate_new_merchant_fraud(user_id: int, start_date: datetime) -> list[tuple[Transaction, str]]:
    """Rule 4 — new-merchant risk: first-time merchant at an above-typical
    first-purchase amount. These merchant names never appear in baseline
    data, so they're guaranteed first-time-for-this-user by construction.
    """
    results = []
    plants = [
        (22, "shopping", "Precision Optics Co.", Decimal("610.00")),
        (55, "health", "Riverside Wellness Spa", Decimal("340.00")),
        (91, "shopping", "Nordic Home Furnishings", Decimal("780.00")),
    ]
    for day_offset, category, merchant, amount in plants:
        day = start_date + timedelta(days=day_offset)
        tx = make_transaction(user_id, random_timestamp(day), merchant, category, amount, jittered_home_location())
        results.append((tx, f"new merchant: first-ever charge from {merchant!r} at above-typical amount"))
    return results


def generate_multi_rule_fraud(user_id: int, start_date: datetime) -> list[tuple[Transaction, str]]:
    """A single transaction that trips two rules at once: new merchant *and*
    amount deviation, as required for a realistic multi-signal case.
    """
    day = start_date + timedelta(days=66)
    category = "shopping"
    merchant = "Aurora Fine Jewelers"
    amount = Decimal("2150.00")  # both first-ever merchant and ~37x category mean
    tx = make_transaction(user_id, random_timestamp(day), merchant, category, amount, jittered_home_location())
    return [(tx, "multi-rule: new merchant AND amount deviation in the same transaction")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="skip the dev-database confirmation prompt")
    args = parser.parse_args()

    confirm_dev_database(skip_prompt=args.yes)
    random.seed(SEED)

    db = SessionLocal()
    try:
        user = get_or_create_seed_user(db)
        deleted = db.query(Transaction).filter(Transaction.user_id == user.id).delete(synchronize_session=False)

        start_date = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=DAYS_OF_HISTORY - 1)

        baseline = generate_baseline(user.id, start_date)

        fraud_groups: list[tuple[str, list[tuple[Transaction, str]]]] = [
            ("velocity", generate_velocity_fraud(user.id, start_date)),
            ("geographic anomaly", generate_geo_anomaly_fraud(user.id, start_date)),
            ("amount deviation", generate_amount_deviation_fraud(user.id, start_date)),
            ("new merchant", generate_new_merchant_fraud(user.id, start_date)),
            ("multi-rule", generate_multi_rule_fraud(user.id, start_date)),
        ]

        db.add_all(baseline)
        for _, rows in fraud_groups:
            db.add_all(tx for tx, _ in rows)
        db.flush()  # assigns PKs without expiring attributes, unlike commit()

        # Capture everything the summary needs now, while ORM objects are still
        # populated — db.commit() below expires all attributes by default
        # (expire_on_commit), which would otherwise trigger a fresh SELECT per
        # row (plus one for `user`) just to print them.
        user_id, user_name = user.id, user.name
        total = len(baseline) + sum(len(rows) for _, rows in fraud_groups)
        summary_lines = []
        for label, rows in fraud_groups:
            summary_lines.append(f"  {label}: {len(rows)} row(s)")
            for tx, reason in rows:
                summary_lines.append(
                    f"    id={tx.id} {tx.timestamp:%Y-%m-%d %H:%M} {tx.merchant!r} "
                    f"${tx.amount} [{tx.location_label}] — {reason}"
                )

        db.commit()

        print(f"Cleared {deleted} existing transaction(s) for user {user_id} ({user_name!r}).")
        print(f"Inserted {total} transactions ({len(baseline)} baseline + "
              f"{total - len(baseline)} planted fraud) spanning {DAYS_OF_HISTORY} days.\n")
        print("Planted fraud scenarios:")
        print("\n".join(summary_lines))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
