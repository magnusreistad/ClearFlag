from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import DISPOSITION_NOTE_MAX_LENGTH

# Mirrors app.models.DISPOSITION_VALUES (and the table's CHECK constraint);
# tests/test_dispositions.py asserts the two stay identical.
DispositionValue = Literal["confirmed_fraud", "false_positive", "ignored"]


class CurrentDisposition(BaseModel):
    """A transaction's latest disposition (SCRUM-29), as GET /transactions
    shows it -- the rule_names snapshot stays on the disposition row for
    the dashboard, not on the feed."""

    value: DispositionValue
    note: str | None
    created_at: datetime


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    timestamp: datetime
    merchant: str
    category: str
    amount: Decimal
    latitude: float
    longitude: float
    location_label: str
    is_flagged: bool
    rationale: str | None
    rule_names: list[str]
    # None when the transaction has never been dispositioned. Shown even if
    # the transaction has since stopped being flagged -- the judgment still
    # happened.
    disposition: CurrentDisposition | None


class TransactionListResponse(BaseModel):
    items: list[TransactionOut]
    total: int
    limit: int
    offset: int


class DispositionIn(BaseModel):
    """Body of POST /transactions/{transaction_id}/disposition (SCRUM-29)."""

    disposition: DispositionValue
    note: str | None = Field(None, max_length=DISPOSITION_NOTE_MAX_LENGTH)

    @field_validator("note")
    @classmethod
    def blank_note_is_no_note(cls, note: str | None) -> str | None:
        # A whitespace-only note carries nothing, so it's stored as NULL
        # rather than as a string a reader would have to strip to discover
        # is empty. A real note is stored exactly as written.
        return note if note and note.strip() else None


class DispositionCounts(BaseModel):
    """SCRUM-32. Currently flagged transactions by their latest
    disposition; the four counts sum to SummaryResponse.flagged_count."""

    confirmed_fraud: int
    false_positive: int
    ignored: int
    unreviewed: int


class UnflaggedDispositionCounts(BaseModel):
    """SCRUM-32. Latest dispositions on transactions that are no longer
    flagged -- the judgment happened, but it's outside the current flag
    set, so it's kept out of fp_rate and coverage."""

    confirmed_fraud: int
    false_positive: int
    ignored: int


class TrendBucket(BaseModel):
    """SCRUM-32. One ISO week (Monday start, UTC) of currently flagged
    transactions, bucketed by transactions.timestamp. The four disposition
    counts sum to `flagged`."""

    week_start: date
    flagged: int
    confirmed_fraud: int
    false_positive: int
    ignored: int
    unreviewed: int


class SummaryResponse(BaseModel):
    """Body of GET /transactions/summary (SCRUM-32)."""

    # Distinct flagged transactions, vs. rule_hit_count's flag rows (one
    # per rule that fired, so a transaction can count more than once).
    flagged_count: int
    rule_hit_count: int
    dispositions: DispositionCounts
    # Flagged transactions with any latest disposition, ignored included.
    judged_count: int
    # false_positive / (false_positive + confirmed_fraud) over currently
    # flagged transactions; None (not 0) when neither has been recorded.
    fp_rate: float | None
    # judged_count / flagged_count; None when nothing is flagged.
    coverage: float | None
    unflagged_dispositions: UnflaggedDispositionCounts
    # Every week from the user's first transaction to their last,
    # zero-filled; empty only when the user has no transactions.
    trend: list[TrendBucket]


class DispositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transaction_id: int
    disposition: DispositionValue
    note: str | None
    rule_names: list[str]
    created_at: datetime
