"""SCRUM-32: GET /transactions/summary. Exact-number tests against a
fixture built here (never seed data), plus the zero cases, user scoping,
and agreement with the feed.

The main fixture is shaped so each likely aggregation bug moves fp_rate to
a different value: earliest-disposition-wins gives 1.0, counting `ignored`
in the denominator gives 0.6, counting the no-longer-flagged transaction's
disposition gives 0.8 -- instead of the correct 0.75.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from app.database import SessionLocal as TestingSessionLocal
from app.main import app
from app.models import Transaction, TransactionDisposition, User

client = TestClient(app)

# A Monday, so ISO week buckets start exactly on WEEK_0, WEEK_0 + 7 days, ...
WEEK_0 = datetime(2026, 3, 2, tzinfo=UTC)

CATEGORIES = [f"category-{i}" for i in range(9)]


def make_transaction(
    user_id: int, category: str, timestamp: datetime, amount: str
) -> Transaction:
    # One merchant and one location for every row, so only amount_deviation
    # can fire (no new merchants, no travel); rows are hours apart, so
    # velocity can't either.
    return Transaction(
        user_id=user_id,
        timestamp=timestamp,
        merchant="Test Merchant",
        category=category,
        amount=Decimal(amount),
        latitude=47.6062,
        longitude=-122.3321,
        location_label="Seattle, WA",
    )


def seed_user(name: str, outliers: dict[str, datetime]) -> tuple[int, dict[str, int]]:
    """Five $10 rows in every category during WEEK_0, then one $500 row
    per category in `outliers` at the given time, which amount_deviation
    flags. Returns (user_id, {category: outlier id})."""
    db = TestingSessionLocal()
    user = User(name=name)
    db.add(user)
    db.flush()
    history = [
        make_transaction(
            user.id, category, WEEK_0 + timedelta(hours=2 * (5 * c + i)), "10.00"
        )
        for c, category in enumerate(CATEGORIES)
        for i in range(5)
    ]
    outlier_rows = {
        category: make_transaction(user.id, category, timestamp, "500.00")
        for category, timestamp in outliers.items()
    }
    db.add_all([*history, *outlier_rows.values()])
    db.commit()
    ids = (user.id, {category: t.id for category, t in outlier_rows.items()})
    db.close()
    return ids


def add_disposition(transaction_id: int, disposition: str, days: int) -> None:
    """Inserted directly, with an explicit created_at, so "latest" is
    decided by created_at alone (not by the id tie-break)."""
    db = TestingSessionLocal()
    db.add(
        TransactionDisposition(
            transaction_id=transaction_id,
            disposition=disposition,
            rule_names=["amount_deviation"],
            created_at=WEEK_0 + timedelta(days=days),
        )
    )
    db.commit()
    db.close()


def get_summary(user_id: int) -> dict:
    response = client.get("/transactions/summary", params={"user_id": user_id})
    assert response.status_code == 200
    return response.json()


def week(start: date, **counts) -> dict:
    return {
        "week_start": start.isoformat(),
        "flagged": 0,
        "confirmed_fraud": 0,
        "false_positive": 0,
        "ignored": 0,
        "unreviewed": 0,
        **counts,
    }


def seed_judged_user(name: str = "Summary User") -> int:
    """The exact-number fixture. Nine outliers T1..T9:

    T1        false_positive, then confirmed_fraud -> CF (latest wins)
    T2-T4     false_positive                       -> FP x3
    T5        ignored
    T6-T8     no disposition                       -> unreviewed x3
    T9        flagged and judged false_positive, then lowered to $10 so it's
              no longer flagged -> unflagged_dispositions only

    T1-T4 fall in WEEK_0 + 1, T5-T9 in WEEK_0 + 3; WEEK_0 has only
    unflagged history and WEEK_0 + 2 has no transactions at all.
    """
    week_1 = WEEK_0 + timedelta(days=7)
    week_3 = WEEK_0 + timedelta(days=21)
    outliers = {
        category: (week_1 if i < 4 else week_3) + timedelta(hours=i)
        for i, category in enumerate(CATEGORIES)
    }
    user_id, ids = seed_user(name, outliers)
    t = [ids[category] for category in CATEGORIES]

    # T9 through the real flow: flagged by a refresh, judged via POST
    # (which 409s on an unflagged transaction), then made unflagged.
    get_summary(user_id)
    response = client.post(
        f"/transactions/{t[8]}/disposition",
        params={"user_id": user_id},
        json={"disposition": "false_positive"},
    )
    assert response.status_code == 201
    db = TestingSessionLocal()
    db.get(Transaction, t[8]).amount = Decimal("10.00")
    db.commit()
    db.close()

    add_disposition(t[0], "false_positive", days=30)
    add_disposition(t[0], "confirmed_fraud", days=31)
    for transaction_id in t[1:4]:
        add_disposition(transaction_id, "false_positive", days=30)
    add_disposition(t[4], "ignored", days=30)
    return user_id


# --- exact numbers -----------------------------------------------------------


def test_summary_exact_numbers():
    user_id = seed_judged_user()

    summary = get_summary(user_id)

    assert summary["flagged_count"] == 8
    assert summary["rule_hit_count"] == 8
    assert summary["dispositions"] == {
        "confirmed_fraud": 1,
        "false_positive": 3,
        "ignored": 1,
        "unreviewed": 3,
    }
    assert summary["judged_count"] == 5
    # 3 / (3 + 1): T1's latest disposition (CF) wins, T5's `ignored` is out
    # of the denominator, and T9's FP is out because T9 is no longer flagged.
    assert summary["fp_rate"] == 0.75
    assert summary["coverage"] == 0.625
    assert summary["unflagged_dispositions"] == {
        "confirmed_fraud": 0,
        "false_positive": 1,
        "ignored": 0,
    }


def test_summary_trend_is_weekly_and_zero_filled():
    user_id = seed_judged_user()

    assert get_summary(user_id)["trend"] == [
        week(date(2026, 3, 2)),
        week(date(2026, 3, 9), flagged=4, confirmed_fraud=1, false_positive=3),
        week(date(2026, 3, 16)),
        week(date(2026, 3, 23), flagged=4, ignored=1, unreviewed=3),
    ]


# --- zero cases ----------------------------------------------------------------


def test_summary_with_flags_but_no_judgments():
    user_id, _ = seed_user("Unjudged User", {CATEGORIES[0]: WEEK_0 + timedelta(days=7)})

    summary = get_summary(user_id)

    assert summary["flagged_count"] == 1
    assert summary["judged_count"] == 0
    assert summary["dispositions"] == {
        "confirmed_fraud": 0,
        "false_positive": 0,
        "ignored": 0,
        "unreviewed": 1,
    }
    assert summary["fp_rate"] is None
    assert summary["coverage"] == 0.0


def test_summary_with_no_flags():
    user_id, _ = seed_user("Unflagged User", {})

    summary = get_summary(user_id)

    assert summary["flagged_count"] == 0
    assert summary["rule_hit_count"] == 0
    assert summary["judged_count"] == 0
    assert summary["fp_rate"] is None
    assert summary["coverage"] is None
    assert summary["trend"] == [week(date(2026, 3, 2))]


def test_summary_with_no_transactions():
    db = TestingSessionLocal()
    user = User(name="Empty User")
    db.add(user)
    db.commit()
    user_id = user.id
    db.close()

    summary = get_summary(user_id)

    assert summary["flagged_count"] == 0
    assert summary["fp_rate"] is None
    assert summary["coverage"] is None
    assert summary["trend"] == []


# --- scoping and freshness -----------------------------------------------------


def test_summary_is_scoped_to_the_user():
    user_id = seed_judged_user()
    other_id, other_ids = seed_user(
        "Other User", {CATEGORIES[0]: WEEK_0 + timedelta(days=14)}
    )
    add_disposition(other_ids[CATEGORIES[0]], "confirmed_fraud", days=30)

    summary = get_summary(user_id)
    other = get_summary(other_id)

    assert (summary["flagged_count"], summary["fp_rate"]) == (8, 0.75)
    assert len(summary["trend"]) == 4
    assert other["flagged_count"] == 1
    assert other["dispositions"]["confirmed_fraud"] == 1
    assert other["fp_rate"] == 0.0
    assert other["coverage"] == 1.0
    assert other["unflagged_dispositions"]["false_positive"] == 0


def test_summary_agrees_with_the_feed():
    user_id = seed_judged_user()

    summary = get_summary(user_id)
    response = client.get("/transactions", params={"user_id": user_id, "limit": 200})
    assert response.status_code == 200
    items = response.json()["items"]

    flagged = [item for item in items if item["is_flagged"]]
    assert summary["flagged_count"] == len(flagged)
    assert summary["rule_hit_count"] == sum(len(item["rule_names"]) for item in flagged)
    for value in ("confirmed_fraud", "false_positive", "ignored"):
        assert summary["dispositions"][value] == sum(
            1
            for item in flagged
            if item["disposition"] and item["disposition"]["value"] == value
        )
    assert summary["dispositions"]["unreviewed"] == sum(
        1 for item in flagged if item["disposition"] is None
    )


def test_summary_refreshes_flags_without_a_feed_load():
    """The summary runs the same flag refresh as the feed, so a change to a
    transaction shows up even if GET /transactions never runs again."""
    user_id, ids = seed_user("Fresh User", {CATEGORIES[0]: WEEK_0 + timedelta(days=7)})
    assert get_summary(user_id)["flagged_count"] == 1

    db = TestingSessionLocal()
    db.get(Transaction, ids[CATEGORIES[0]]).amount = Decimal("10.00")
    db.commit()
    db.close()

    assert get_summary(user_id)["flagged_count"] == 0
