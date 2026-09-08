from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.models import Transaction
from app.rules.amount_deviation import evaluate_amount_deviation
from app.rules.geographic_anomaly import evaluate_geographic_anomaly
from app.rules.new_merchant_risk import evaluate_new_merchant_risk
from app.rules.velocity import evaluate_velocity

# SCRUM-61: order mirrors the Rules Engine Design Doc's rule table, so a
# transaction's concatenated multi-rule rationale always reads in the same
# order regardless of which rules fired.
RULES = {
    "velocity": evaluate_velocity,
    "geographic_anomaly": evaluate_geographic_anomaly,
    "amount_deviation": evaluate_amount_deviation,
    "new_merchant_risk": evaluate_new_merchant_risk,
}


@dataclass(frozen=True)
class FlagHit:
    transaction_id: int
    rule_name: str
    rationale: str


def evaluate_all_rules(transactions: Sequence[Transaction]) -> list[FlagHit]:
    """Run every rule against `transactions` and return each hit tagged with
    the rule that produced it.

    Expects `transactions` to already be scoped to a single user, same as
    each individual evaluate_* function.
    """
    return [
        FlagHit(transaction_id=hit.transaction_id, rule_name=rule_name, rationale=hit.rationale)
        for rule_name, evaluate in RULES.items()
        for hit in evaluate(transactions)
    ]


def concatenate_rationales(hits: Sequence[FlagHit]) -> dict[int, str]:
    """Merge hits by transaction_id into one rationale string per flagged
    transaction, concatenating every triggered reason (per the Rules Engine
    Design Doc's Multi-Rule Flags section: show all the reasoning, not just
    the top hit). Transactions with no hits are absent from the result.
    """
    rationales_by_transaction: dict[int, list[str]] = defaultdict(list)
    for hit in hits:
        rationales_by_transaction[hit.transaction_id].append(hit.rationale)
    return {
        transaction_id: " ".join(rationales)
        for transaction_id, rationales in rationales_by_transaction.items()
    }
