"""SCRUM-68. Builds the payload shape app.investigation_agent.validation's
validate_rationale expects (see that module's docstring), from a flagged
transaction's own data and the rules engine's per-rule structured values
(RuleHit.values, threaded through app.rules.engine.FlagHit and grouped by
app.rules.engine.rule_values_by_transaction).

Pure dict construction -- no computation of its own, and nothing here
queries a database or calls a model. Not wired into investigation_graph yet
(that's SCRUM-53's job): this exists so SCRUM-53's future composition step
and this ticket's own cross-check tests share one place that builds the
shape validate_rationale reads, rather than each hand-rolling it.
"""

from typing import Any

from app.investigation_agent.state import TransactionData

# The flagged transaction's own fields validation.py's payload docstring
# lists as citable -- everything except timestamp, which _flatten_facts
# already drops as non-citable, so leaving it out here just keeps the
# snapshot matching that documented shape exactly.
_TRANSACTION_SNAPSHOT_KEYS = (
    "id",
    "user_id",
    "merchant",
    "category",
    "amount",
    "location_label",
    "latitude",
    "longitude",
)


def build_payload(
    transaction: TransactionData,
    rule_names: list[str],
    rule_values: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """`rule_values` is keyed by rule name -- e.g. the per-transaction entry
    from app.rules.engine.rule_values_by_transaction(), each value that
    rule's RuleHit.values for this transaction.
    """
    return {
        "transaction": {key: transaction[key] for key in _TRANSACTION_SNAPSHOT_KEYS},
        "rule_names": list(rule_names),
        "rules": dict(rule_values),
    }
