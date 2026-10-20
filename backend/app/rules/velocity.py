from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.models import Transaction

# SCRUM-17 defaults: more than 5 transactions within a 10-minute rolling
# window is flagged. Chosen to catch tight card-testing-style bursts while
# tolerating normal rapid spending (e.g. gas + coffee + a store).
DEFAULT_MAX_COUNT = 5
DEFAULT_WINDOW_MINUTES = 10


@dataclass(frozen=True)
class RuleHit:
    transaction_id: int
    rationale: str
    # SCRUM-68: the same numbers rationale bakes into prose, exposed
    # structurally so an Investigation Agent payload can cite them. No value
    # here is computed for this field alone -- both keys are already local
    # variables the rationale f-string below reads from.
    values: dict[str, Any] = field(default_factory=dict)


def evaluate_velocity(
    transactions: Sequence[Transaction],
    *,
    max_count: int = DEFAULT_MAX_COUNT,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> list[RuleHit]:
    """Flag every transaction whose own window -- the `window_minutes` ending
    at its timestamp, inclusive of both ends and of the transaction itself --
    holds more than `max_count` transactions (assumed to all belong to the
    same user).

    SCRUM-76: only that window counts, never one ending after the
    transaction, so a flag uses only what was known when it happened
    (real-time scoring). It is exactly get_transaction_history's window
    (timestamp >= anchor - window and <= anchor), so the tool's count always
    equals the rule's -- including transactions that share the anchor's
    timestamp, which the tool's `<=` counts too.

    Expects `transactions` to already be scoped to a single user.
    """
    window = timedelta(minutes=window_minutes)
    ordered = sorted(transactions, key=lambda t: t.timestamp)
    timestamps = [t.timestamp for t in ordered]

    hits = []
    for t in ordered:
        count = bisect_right(timestamps, t.timestamp) - bisect_left(
            timestamps, t.timestamp - window
        )
        if count > max_count:
            hits.append(
                RuleHit(
                    transaction_id=t.id,
                    rationale=(
                        f"Flagged: You made {count} transactions "
                        f"in {window_minutes} minutes, which is unusual for your "
                        f"account."
                    ),
                    values={
                        "transaction_count": count,
                        "window_minutes": window_minutes,
                    },
                )
            )
    return hits
