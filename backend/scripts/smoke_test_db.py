"""Manual smoke test against the real Neon database.

Inserts one seeded user and one transaction, reads them back, and checks
that the connection works and that types (Decimal for amount, tz-aware
datetime for timestamp) round-trip correctly. Cleans up after itself.

Run with: python -m scripts.smoke_test_db
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.database import SessionLocal
from app.models import Transaction, User


def main() -> None:
    db = SessionLocal()
    try:
        user = User(name="Smoke Test User")
        db.add(user)
        db.flush()

        transaction = Transaction(
            user_id=user.id,
            timestamp=datetime.now(timezone.utc),
            merchant="Test Merchant",
            category="groceries",
            amount=Decimal("42.50"),
            latitude=59.9139,
            longitude=10.7522,
            location_label="Oslo, Norway",
        )
        db.add(transaction)
        db.commit()

        db.refresh(user)
        db.refresh(transaction)

        fetched_user = db.get(User, user.id)
        fetched_transaction = db.get(Transaction, transaction.id)

        assert fetched_user is not None
        assert fetched_transaction is not None
        assert fetched_transaction.user_id == fetched_user.id

        assert isinstance(fetched_transaction.amount, Decimal), (
            f"expected Decimal, got {type(fetched_transaction.amount)}"
        )
        assert fetched_transaction.amount == Decimal("42.50")
        assert isinstance(fetched_transaction.latitude, float)
        assert isinstance(fetched_transaction.timestamp, datetime)

        print("Smoke test passed:")
        print(f"  user:        {fetched_user.id} {fetched_user.name!r}")
        print(
            f"  transaction: {fetched_transaction.id} "
            f"{fetched_transaction.amount!r} ({type(fetched_transaction.amount).__name__}) "
            f"at {fetched_transaction.location_label!r}"
        )

        db.delete(transaction)
        db.delete(user)
        db.commit()
        print("Cleaned up seeded rows.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
