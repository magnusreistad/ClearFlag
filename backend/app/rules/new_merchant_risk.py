from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import mean, stdev

from app.models import Transaction

# SCRUM-20 defaults: mirror amount deviation's (SCRUM-19) z-score approach,
# but applied to the sequence of a user's first-ever purchases at each new
# merchant, rather than to per-category spend. The design doc's wording --
# "higher than your typical first-time purchase" -- points at comparing a
# new first purchase against the user's *other* first purchases, not their
# overall average transaction: overall average would flag someone simply
# spending more than usual generally, which isn't what this rule is about.
DEFAULT_STD_DEV_THRESHOLD = 3

# Need enough prior first-time-merchant purchases for "typical first-time
# purchase" to mean anything. Mirrors amount deviation's MIN_HISTORY_COUNT.
MIN_HISTORY_COUNT = 5

# Floor for the denominator in the z-score, same rationale as amount
# deviation: a run of near-identical first-purchase amounts (e.g. several
# $9.99 trial subscriptions at different merchants) would otherwise blow up
# the z-score on a tiny absolute jump.
MIN_STD_DEV_FLOOR = Decimal("1.00")


@dataclass(frozen=True)
class RuleHit:
    transaction_id: int
    rationale: str


def evaluate_new_merchant_risk(
    transactions: Sequence[Transaction],
    *,
    std_dev_threshold: int = DEFAULT_STD_DEV_THRESHOLD,
    min_history: int = MIN_HISTORY_COUNT,
) -> list[RuleHit]:
    """Flag a transaction when it's the user's first-ever transaction with a
    given merchant AND its amount is more than `std_dev_threshold` standard
    deviations above the user's own historical mean *first-time purchase*
    amount.

    Transactions are walked in timestamp order. The first transaction seen
    for a merchant is that merchant's "first-time purchase"; every later
    transaction at the same merchant is a repeat and can never be flagged
    by this rule. Each first-time purchase is compared only against the
    first-time purchases that precede it -- its "typical first-time
    purchase" as of that point in time. A first-time purchase is skipped
    (never flagged) until at least `min_history` earlier first-time
    purchases exist.

    Expects `transactions` to already be scoped to a single user.
    """
    ordered = sorted(transactions, key=lambda t: t.timestamp)

    seen_merchants: set[str] = set()
    first_purchases: list[Transaction] = []
    for t in ordered:
        if t.merchant not in seen_merchants:
            seen_merchants.add(t.merchant)
            first_purchases.append(t)

    hits: list[RuleHit] = []
    for i, t in enumerate(first_purchases):
        history = first_purchases[:i]
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
                        f"Flagged: this is your first purchase from this merchant, "
                        f"and the amount is {pct_higher:.0f}% higher than your "
                        f"typical first-time purchase."
                    ),
                )
            )

    return hits
