from datetime import datetime
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


class DispositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transaction_id: int
    disposition: DispositionValue
    note: str | None
    rule_names: list[str]
    created_at: datetime
