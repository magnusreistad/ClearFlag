from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import mean, stdev

from app.models import Transaction

# SCRUM-19 defaults: flag a transaction once it's more than 3 standard
# deviations above the user's own historical mean spend in that category.
# 3 stdev is the conventional statistical outlier cutoff (~99.7% of a
# normal distribution falls within 3 stdev of the mean), which keeps the
# rule tolerant of a merchant's normal price variation (a bigger grocery
# run, a pricier restaurant visit) while still catching genuinely
# unusual spend -- the planted fraud scenarios in seed_transactions.py
# (e.g. $540 against a $62 category mean) clear this bar by a wide margin.
DEFAULT_STD_DEV_THRESHOLD = 3

# Need enough prior transactions in a category for "average" and "standard
# deviation" to mean anything. 5 mirrors the velocity rule's threshold and
# is a reasonable floor for a sample stdev estimate.
MIN_HISTORY_COUNT = 5

# Floor for the denominator in the z-score. A user with a run of
# identical (or near-identical) past amounts in a category -- e.g. the
# same $9.99 subscription five times -- has a historical stdev of ~0,
# which would make any tiny deviation register as an enormous number of
# "standard deviations" and trip the rule on noise. Flooring the stdev at
# $1 means only a genuinely large jump still clears the threshold.
MIN_STD_DEV_FLOOR = Decimal("1.00")


@dataclass(frozen=True)
class RuleHit:
    transaction_id: int
    rationale: str


def evaluate_amount_deviation(
    transactions: Sequence[Transaction],
    *,
    std_dev_threshold: int = DEFAULT_STD_DEV_THRESHOLD,
    min_history: int = MIN_HISTORY_COUNT,
) -> list[RuleHit]:
    """Flag a transaction when its amount is more than `std_dev_threshold`
    standard deviations above the user's own historical mean spend in that
    transaction's category.

    For each category, transactions are walked in timestamp order and each
    one is compared only against the transactions that precede it in that
    category -- its "historical average" as of that point in time. A
    transaction is skipped (never flagged) until at least `min_history`
    prior transactions exist in its category.

    Only unusually *high* amounts are flagged -- an unusually low amount
    isn't a fraud signal.

    Expects `transactions` to already be scoped to a single user.
    """
    by_category: dict[str, list[Transaction]] = defaultdict(list)
    for t in transactions:
        by_category[t.category].append(t)

    hits: list[RuleHit] = []
    for txns in by_category.values():
        ordered = sorted(txns, key=lambda t: t.timestamp)
        for i, t in enumerate(ordered):
            history = ordered[:i]
            if len(history) < min_history:
                continue

            historical_amounts = [h.amount for h in history]
            historical_mean = mean(historical_amounts)
            if t.amount <= historical_mean:
                continue

            historical_stdev = max(stdev(historical_amounts), MIN_STD_DEV_FLOOR)
            z_score = (t.amount - historical_mean) / historical_stdev
            if z_score > std_dev_threshold:
                pct_higher = (t.amount - historical_mean) / historical_mean * 100
                hits.append(
                    RuleHit(
                        transaction_id=t.id,
                        rationale=(
                            f"Flagged: this amount is {pct_higher:.0f}% higher "
                            f"than your typical spend in this category."
                        ),
                    )
                )

    return hits
