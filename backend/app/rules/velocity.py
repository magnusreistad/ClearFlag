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
    """Flag every transaction that belongs to at least one over-threshold
    window: more than `max_count` transactions (assumed to all belong to the
    same user) within a `window_minutes` rolling window ending at that
    transaction's timestamp. If a transaction falls in more than one
    over-threshold window, its rationale uses the largest count seen.

    Expects `transactions` to already be scoped to a single user.
    """
    window = timedelta(minutes=window_minutes)
    ordered = sorted(transactions, key=lambda t: t.timestamp)

    # Every member of an over-threshold window must be flagged, not just the
    # transaction that tipped it over, so each window's members are walked
    # explicitly below. That makes this worst-case O(n^2) for a single burst
    # spanning the whole list -- an acceptable trade-off at this dataset's scale.
    best_count_by_id: dict[int, int] = {}
    start = 0
    for end in range(len(ordered)):
        while ordered[end].timestamp - ordered[start].timestamp > window:
            start += 1
        count_in_window = end - start + 1
        if count_in_window > max_count:
            for i in range(start, end + 1):
                tx_id = ordered[i].id
                best_count_by_id[tx_id] = max(best_count_by_id.get(tx_id, 0), count_in_window)

    return [
        RuleHit(
            transaction_id=t.id,
            rationale=(
                f"Flagged: You made {best_count_by_id[t.id]} transactions "
                f"in {window_minutes} minutes, which is unusual for your "
                f"account."
            ),
            values={
                "transaction_count": best_count_by_id[t.id],
                "window_minutes": window_minutes,
            },
        )
        for t in ordered
        if t.id in best_count_by_id
    ]
