from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean, stdev

from sqlalchemy.orm import Session, sessionmaker

from app.database import engine
from app.models import Transaction, User
from app.rules.geographic_anomaly import (
    MIN_STD_DEV_FLOOR_MILES,
    _centroid,
    _haversine_miles,
    evaluate_geographic_anomaly,
)

# expire_on_commit=False: unlike test_transactions.py, these tests hand ORM
# objects straight to evaluate_geographic_anomaly() after the session
# closes, so their attributes must stay readable without a live session to
# refresh from.
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_transaction(
    user_id: int,
    timestamp: datetime,
    lat: float,
    lon: float,
    location_label: str,
    merchant: str = "Test Merchant",
) -> Transaction:
    return Transaction(
        user_id=user_id,
        timestamp=timestamp,
        merchant=merchant,
        category="shopping",
        amount=Decimal("10.00"),
        latitude=lat,
        longitude=lon,
        location_label=location_label,
    )


def new_user(db: Session) -> int:
    user = User(name="Geographic Anomaly Test User")
    db.add(user)
    db.flush()
    return user.id


def add_transaction(db: Session, user_id: int, day_offset: int, lat: float, lon: float, label: str) -> Transaction:
    tx = make_transaction(user_id, BASE_TIME + timedelta(days=day_offset), lat, lon, label)
    db.add(tx)
    db.commit()
    return tx


# A scattered Seattle-metro history (real, distinct locations, not a single
# repeated point) so most tests exercise the actual stdev computation rather
# than only the MIN_STD_DEV_FLOOR_MILES floor.
# centroid ~= (47.62444, -122.2603); historical mean distance from centroid
# ~= 13.10 mi; sample stdev ~= 11.80 mi (already above the 10 mi floor, so
# the floor is inert for this fixture); 3-stdev bound ~= 48.49 mi.
METRO_HISTORY = [
    (47.6062, -122.3321, "Seattle, WA"),
    (47.6101, -122.2015, "Bellevue, WA"),
    (47.2529, -122.4443, "Tacoma, WA"),
    (47.9790, -122.2021, "Everett, WA"),
    (47.6740, -122.1215, "Redmond, WA"),
]


def create_metro_history(db: Session, user_id: int) -> list[Transaction]:
    txns = [
        make_transaction(user_id, BASE_TIME + timedelta(days=i), lat, lon, label)
        for i, (lat, lon, label) in enumerate(METRO_HISTORY)
    ]
    db.add_all(txns)
    db.commit()
    return txns


def test_distance_at_or_below_historical_mean_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    history = create_metro_history(db, user_id)
    # Issaquah, WA: ~12.46 mi from centroid, below the ~13.10 mi historical
    # mean -- an unusually *close* location isn't a fraud signal.
    candidate = add_transaction(db, user_id, len(history), 47.5301, -122.0326, "Issaquah, WA")
    db.close()

    hits = evaluate_geographic_anomaly([*history, candidate])

    assert hits == []


def test_above_mean_but_below_threshold_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    history = create_metro_history(db, user_id)
    # North Bend, WA: ~23.92 mi from centroid, z ~= 0.92 -- above the mean
    # but well under the 3-stdev bar (~48.49 mi).
    candidate = add_transaction(db, user_id, len(history), 47.4915, -121.7867, "North Bend, WA")
    db.close()

    hits = evaluate_geographic_anomaly([*history, candidate])

    assert hits == []


def test_far_outside_cluster_flags():
    db = TestingSessionLocal()
    user_id = new_user(db)
    history = create_metro_history(db, user_id)
    # Portland, OR: ~147 mi from centroid, z ~= 11.36 -- clears the default
    # 3-stdev bar by a wide margin.
    candidate = add_transaction(db, user_id, len(history), 45.5152, -122.6784, "Portland, OR")
    db.close()

    hits = evaluate_geographic_anomaly([*history, candidate])

    assert len(hits) == 1
    assert hits[0].transaction_id == candidate.id


def test_rationale_uses_location_label():
    db = TestingSessionLocal()
    user_id = new_user(db)
    history = create_metro_history(db, user_id)
    candidate = add_transaction(db, user_id, len(history), 45.5152, -122.6784, "Portland, OR")
    db.close()

    hits = evaluate_geographic_anomaly([*history, candidate])

    assert hits[0].rationale == (
        "Flagged: This transaction occurred in Portland, OR, far from where you usually shop."
    )


def test_values_populated_with_expected_keys_and_correct_figures():
    """SCRUM-68: values carries location_label (the entity the rationale
    string cites) plus distance_miles/distance_mean_miles/distance_stdev_miles
    -- this hit's own distance and the historical distribution it was
    compared against -- verified against _centroid/_haversine_miles/stdev
    directly rather than hand-typed. distance_miles is a legitimate payload
    fact even though get_geo_distance (this rule's mapped tool) computes the
    same figure as evidence -- see RuleHit.values' comment; TestErroredToolNotCitable
    in tests/test_investigation_agent_validation.py covers facts that stay
    tool-only (typical_location_label, distance_km)."""
    db = TestingSessionLocal()
    user_id = new_user(db)
    history = create_metro_history(db, user_id)
    candidate = add_transaction(db, user_id, len(history), 45.5152, -122.6784, "Portland, OR")
    db.close()

    hits = evaluate_geographic_anomaly([*history, candidate])

    centroid_lat, centroid_lon = _centroid(history)
    historical_distances = [_haversine_miles(centroid_lat, centroid_lon, h.latitude, h.longitude) for h in history]
    expected_current_distance = _haversine_miles(centroid_lat, centroid_lon, candidate.latitude, candidate.longitude)
    expected_mean = mean(historical_distances)
    expected_stdev = max(stdev(historical_distances), MIN_STD_DEV_FLOOR_MILES)

    assert hits[0].values == {
        "location_label": "Portland, OR",
        "distance_miles": expected_current_distance,
        "distance_mean_miles": expected_mean,
        "distance_stdev_miles": expected_stdev,
    }


def test_too_few_historical_locations_does_not_flag():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # Only 4 prior locations -- below MIN_HISTORY_COUNT of 5 -- however far
    # the 5th one is.
    history = [add_transaction(db, user_id, i, lat, lon, label) for i, (lat, lon, label) in enumerate(METRO_HISTORY[:4])]
    candidate = add_transaction(db, user_id, 10, 13.7563, 100.5018, "Bangkok, Thailand")
    db.close()

    hits = evaluate_geographic_anomaly([*history, candidate])

    assert hits == []


def test_single_transaction_returns_empty():
    db = TestingSessionLocal()
    user_id = new_user(db)
    txns = [add_transaction(db, user_id, 0, 47.6062, -122.3321, "Seattle, WA")]
    db.close()

    assert evaluate_geographic_anomaly(txns) == []


def test_empty_transaction_list_returns_empty():
    assert evaluate_geographic_anomaly([]) == []


def test_zero_variance_history_does_not_explode_on_tiny_deviation():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # Five identical prior locations (stdev=0), then a ~2 mi bump -- small in
    # absolute terms, but "infinite" stdevs without the floor.
    history = [add_transaction(db, user_id, i, 47.6062, -122.3321, "Seattle, WA") for i in range(5)]
    candidate = add_transaction(db, user_id, 5, 47.6350, -122.3321, "North Seattle, WA")
    db.close()

    hits = evaluate_geographic_anomaly([*history, candidate])

    assert hits == []


def test_zero_variance_history_still_flags_large_deviation():
    db = TestingSessionLocal()
    user_id = new_user(db)
    history = [add_transaction(db, user_id, i, 47.6062, -122.3321, "Seattle, WA") for i in range(5)]
    candidate = add_transaction(db, user_id, 5, 13.7563, 100.5018, "Bangkok, Thailand")
    db.close()

    hits = evaluate_geographic_anomaly([*history, candidate])

    assert len(hits) == 1
    assert hits[0].transaction_id == candidate.id


def test_unsorted_input_still_correct():
    db = TestingSessionLocal()
    user_id = new_user(db)
    history = create_metro_history(db, user_id)
    candidate = add_transaction(db, user_id, len(history), 45.5152, -122.6784, "Portland, OR")
    db.close()
    shuffled = list(reversed([*history, candidate]))

    hits = evaluate_geographic_anomaly(shuffled)

    assert len(hits) == 1
    assert hits[0].transaction_id == candidate.id


def test_custom_threshold_narrows_what_flags():
    db = TestingSessionLocal()
    user_id = new_user(db)
    # ~15.2 mi bump against a zero-variance (floored at 10 mi) history: z ~=
    # 1.52 -- under the default 3-stdev bar but over a tighter 1-stdev bar.
    history = [add_transaction(db, user_id, i, 47.6062, -122.3321, "Seattle, WA") for i in range(5)]
    candidate = add_transaction(db, user_id, 5, 47.8262, -122.3321, "Nearby Town, WA")
    db.close()
    txns = [*history, candidate]

    assert evaluate_geographic_anomaly(txns, std_dev_threshold=3) == []
    hits = evaluate_geographic_anomaly(txns, std_dev_threshold=1)
    assert len(hits) == 1
    assert hits[0].transaction_id == candidate.id
