from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    """Minimal, single-seeded-row table for MVP.

    No auth/login (explicitly out of scope for this project). This exists as a
    scoping anchor — per the Decision Log ("User Scope: Seeded Users Table vs.
    Implicit Single User") — so every query can be written per-user from the
    start, rather than hardcoding a single implicit user that would need a
    migration later if multi-user support is ever added.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, default="Demo User")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # Velocity rule: rolling-window counts of transactions per user.
        Index("ix_transactions_user_id_timestamp", "user_id", "timestamp"),
        # New-merchant risk rule: first-time-merchant lookups per user.
        Index("ix_transactions_user_id_merchant", "user_id", "merchant"),
        # Amount deviation rule: per-category average lookups per user.
        Index("ix_transactions_user_id_category", "user_id", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    merchant: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    # Numeric(8, 2), not Float: money needs fixed-precision decimal arithmetic,
    # not binary floating-point, which can't represent amounts like 0.10 exactly
    # and accumulates rounding error across sums/comparisons.
    amount: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    location_label: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="transactions")
