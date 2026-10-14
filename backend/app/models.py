from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import ARRAY
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
    flags: Mapped[list["TransactionFlag"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )


class TransactionFlag(Base):
    """One row per rule hit (SCRUM-61). Rules aren't incremental -- each one
    needs a user's full transaction history to evaluate correctly -- so
    these rows are wholesale recomputed (deleted and reinserted) every time
    the rules engine runs for a user, rather than updated in place. See
    app/routers/transactions.py.
    """

    __tablename__ = "transaction_flags"
    __table_args__ = (
        Index("ix_transaction_flags_transaction_id", "transaction_id"),
        # Safety net against the delete-then-reinsert refresh (see
        # app/routers/transactions.py) double-counting a rule hit if it's
        # ever interrupted partway or a future code path skips the delete.
        UniqueConstraint("transaction_id", "rule_name", name="uq_transaction_flags_transaction_rule"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    rule_name: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transaction: Mapped["Transaction"] = relationship(back_populates="flags")


class AgentRationale(Base):
    """SCRUM-53 Phase B. One row per Investigation Agent composition ATTEMPT
    (not one row per transaction) -- this is both the cache and the audit
    record, per the Confluence Decision Log. `scripts.compose_rationales` is
    the only writer, and it runs outside any request path; GET /transactions
    (app/routers/transactions.py's _refresh_flags) only ever reads this
    table, never calls the model, and never writes to it.

    Append-only by convention: application code never updates or deletes a
    row here, a failed composition included -- see validation_passed,
    violations, and composition_error below, all populated on failure too,
    since a failed attempt is part of the audit trail, not noise to discard.

    fact_fingerprint + prompt_version together identify "the exact facts and
    exact prompt version that produced this row" (see
    app.investigation_agent.fingerprint.compute_fact_fingerprint for exactly
    what's hashed, and its documented staleness limitation). The refresh
    path looks up the latest validation_passed=true row matching a flagged
    transaction's CURRENT fingerprint + PROMPT_VERSION in one query across
    every flagged transaction at once (see
    ix_agent_rationales_transaction_fingerprint_prompt_version below); a
    cache miss, a failed-only row, or a stale prompt_version all fall back
    to the interim formatter identically.

    SCRUM-53 follow-up (composed_text, validator_version -- added in a later
    migration than the rest of this table, so both are NULL on every row
    written before they existed; never backfilled, since there's no way to
    recover what an old row's composed text or validator version actually
    was, only guess): composed_text is the audit record of what the model
    actually composed on THIS attempt, pass or fail -- unlike `rationale`
    below, which stays NULL on failure, composed_text is populated whenever
    the model returned any text at all, specifically so a failed attempt's
    text survives for debugging/tuning instead of being discarded. See
    `rationale`'s own comment for why these are two separate columns rather
    than one.
    """

    __tablename__ = "agent_rationales"
    __table_args__ = (
        Index(
            "ix_agent_rationales_transaction_fingerprint_prompt_version",
            "transaction_id",
            "fact_fingerprint",
            "prompt_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    fact_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable, and deliberately NEVER set on a failed validation (stays
    # NULL rather than holding the ungrounded text) -- this is the one
    # column any read path is allowed to treat as "safe to show the account
    # holder". Kept separate from composed_text (below) specifically so
    # that invariant is structural: a future read path that forgets the
    # validation_passed=true filter still can't leak a failed rationale,
    # because a failed row's `rationale` is NULL no matter what it queries
    # for -- the failed attempt's actual text lives ONLY in composed_text,
    # a column no request-serving code path ever reads.
    rationale: Mapped[str | None] = mapped_column(String, nullable=True)
    validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Always a list (possibly empty, on a pass) -- never NULL -- so a reader
    # never has to special-case "no violations" vs. "violations not recorded".
    violations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    composition_error: Mapped[str | None] = mapped_column(String, nullable=True)
    # SCRUM-53 follow-up. The full audit record of what the model composed
    # on THIS attempt, pass or fail -- populated whenever compose_rationale
    # produced any text (NULL only when composition_error is set and no
    # text was ever generated, e.g. a timeout before any response arrived).
    # On a passing row this duplicates `rationale` -- an intentional,
    # harmless redundancy: `rationale` answers "what's safe to serve",
    # composed_text answers "what did this attempt actually produce",
    # and a passing row has the same answer to both.
    composed_text: Mapped[str | None] = mapped_column(String, nullable=True)
    # SCRUM-53 follow-up. app.investigation_agent.validation.VALIDATOR_VERSION
    # at the time THIS row was validated -- bumped whenever a change to the
    # entity/citation heuristic could change which rationales pass or fail
    # (same bump-on-behavior-change convention as PROMPT_VERSION). Lets a
    # later audit distinguish "this failed under an old, since-improved
    # validator" from "this failed under the current one".
    validator_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# The three analyst judgments a flagged transaction can receive (SCRUM-29).
# Kept as a CHECK on a plain String column rather than a native Postgres
# ENUM, so a later value change is an ordinary constraint swap in a
# migration instead of an ALTER TYPE.
DISPOSITION_VALUES = ("confirmed_fraud", "false_positive", "ignored")
DISPOSITION_NOTE_MAX_LENGTH = 2000


class TransactionDisposition(Base):
    """SCRUM-29. One row per analyst judgment of a flagged transaction --
    an event log, not a status column: the CURRENT disposition is the
    latest row for a transaction (created_at DESC, id DESC as the tie-break
    for two rows written in the same database transaction, which share a
    now()).

    Append-only by convention, like agent_rationales: application code
    never updates or deletes a row here (scripts.seed_transactions is the
    one exception -- it clears a user's whole dataset, dispositions
    included, before regenerating it).

    References transactions.id, never transaction_flags.id: _refresh_flags
    deletes and reinserts every TransactionFlag row on every GET
    /transactions (see app/routers/transactions.py), so a flag's id doesn't
    survive a single request. Which rules had fired is instead snapshotted
    into rule_names at POST time, from the persisted flags the analyst was
    looking at -- so a per-rule false-positive rate reflects what was
    actually judged, even after a rule's threshold changes or the
    transaction stops being flagged.
    """

    __tablename__ = "transaction_dispositions"
    __table_args__ = (
        # Latest-disposition-per-transaction lookup (GET /transactions'
        # DISTINCT ON query) -- same ordering as the query, so it's an
        # index scan, not a sort.
        Index(
            "ix_transaction_dispositions_transaction_latest",
            "transaction_id",
            literal_column("created_at").desc(),
            literal_column("id").desc(),
        ),
        CheckConstraint(
            "disposition IN ('confirmed_fraud', 'false_positive', 'ignored')",
            name="ck_transaction_dispositions_disposition_value",
        ),
        CheckConstraint(
            f"char_length(note) <= {DISPOSITION_NOTE_MAX_LENGTH}",
            name="ck_transaction_dispositions_note_length",
        ),
        CheckConstraint(
            "cardinality(rule_names) >= 1",
            name="ck_transaction_dispositions_rule_names_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    disposition: Mapped[str] = mapped_column(String, nullable=False)
    # NULL for "no note" -- the endpoint stores a whitespace-only note as
    # NULL too, so a reader never has to tell "" from "   " from absent.
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    rule_names: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
