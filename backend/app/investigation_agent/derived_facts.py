"""SCRUM-52. The single place any *calculated* (not directly stored) value a
composed rationale may cite gets computed. Both validation.py (this ticket)
and SCRUM-53's composition prompt must use this module rather than doing
their own arithmetic -- the model is never trusted to compute a percentage,
and the validator must check the model's number against the exact same
formula the model was told to use, not a re-derivation that could drift from
it.

Start small: the only derived value the interim formatter (app.rules.
amount_deviation / app.rules.new_merchant_risk) currently computes is
"percent above the category mean" for an amount_deviation flag. Add more
functions here as later rules' formatters gain their own computed values --
never let the validator or the prompt hand-roll a formula that belongs here.
"""

from decimal import Decimal
from typing import Any


def percent_above_category_mean(amount: Decimal, category_mean: Decimal) -> Decimal:
    """Matches app.rules.amount_deviation.evaluate_amount_deviation's
    `pct_higher` formula exactly: (amount - mean) / mean * 100. Also reused
    as-is by app.rules.new_merchant_risk, which computes the same ratio
    against the mean *first-time purchase* amount instead of a category
    mean -- the formula doesn't care which historical mean it's given.
    """
    return (amount - category_mean) / category_mean * 100


def compute_derived_facts(payload: dict[str, Any]) -> dict[str, dict[str, Decimal]]:
    """Computes every derived value citable for this invocation, keyed the
    same way `payload["rules"]` is keyed (by rule name) so validation.py can
    flatten it alongside payload/evidence under a "derived.<rule>.<field>"
    path.

    Silently omits a rule's derived facts if that rule's raw inputs aren't
    present in `payload["rules"]` (e.g. the rule didn't fire this
    invocation) or if category_mean is zero (percent-above-mean is
    undefined) -- never fabricates a value from missing or degenerate
    inputs.
    """
    derived: dict[str, dict[str, Decimal]] = {}

    amount_deviation = payload.get("rules", {}).get("amount_deviation")
    if amount_deviation is not None:
        amount = amount_deviation.get("amount")
        category_mean = amount_deviation.get("category_mean")
        if amount is not None and category_mean:
            derived["amount_deviation"] = {
                "percent_above_mean": percent_above_category_mean(amount, category_mean)
            }

    return derived
