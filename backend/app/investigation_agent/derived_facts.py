"""SCRUM-52. The single place any *calculated* (not directly stored) value a
composed rationale may cite gets computed. Both validation.py (this ticket)
and SCRUM-53's composition prompt must use this module rather than doing
their own arithmetic -- the model is never trusted to compute a percentage,
and the validator must check the model's number against the exact same
formula the model was told to use, not a re-derivation that could drift from
it.

SCRUM-68: app.rules.amount_deviation now exposes percent_above_mean
structurally on RuleHit.values (computed with this exact formula), so
compute_derived_facts below reads that value through rather than
recomputing it -- kept as a "derived" entry, not deleted, so validation.py's
existing "derived.*" flattening path and payload consumers don't need to
change shape. percent_above_category_mean itself stays here as the
canonical formula: still exercised directly by tests, and available for a
future rule/derived value that needs the same ratio but doesn't expose it on
RuleHit.values itself (app.rules.new_merchant_risk now exposes its own
percent_above_mean directly on RuleHit.values too, so it needs no entry
here).
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
    invocation) -- never fabricates a value from missing inputs.
    """
    derived: dict[str, dict[str, Decimal]] = {}

    amount_deviation = payload.get("rules", {}).get("amount_deviation")
    if amount_deviation is not None:
        percent_above_mean = amount_deviation.get("percent_above_mean")
        if percent_above_mean is not None:
            derived["amount_deviation"] = {"percent_above_mean": percent_above_mean}

    return derived
