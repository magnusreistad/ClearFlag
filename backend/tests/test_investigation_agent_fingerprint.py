"""SCRUM-53 Phase B: app.investigation_agent.fingerprint.compute_fact_fingerprint
-- deterministic, sensitive to rule values/transaction snapshot/PROMPT_VERSION,
and structurally incapable of being affected by tool evidence (evidence isn't
part of its input at all). No DB, no LLM, no network.
"""

import inspect
from copy import deepcopy
from decimal import Decimal

import app.investigation_agent.fingerprint as fingerprint_module
from app.investigation_agent.fingerprint import compute_fact_fingerprint

PAYLOAD = {
    "transaction": {
        "id": 843,
        "user_id": 2,
        "merchant": "Meridian Duty-Free Traders",
        "category": "shopping",
        "amount": Decimal("3200.00"),
        "location_label": "Manila, Philippines",
        "latitude": 14.5995,
        "longitude": 120.9842,
    },
    "rule_names": ["amount_deviation"],
    "rules": {
        "amount_deviation": {
            "amount": Decimal("3200.00"),
            "category_mean": Decimal("162.2013333333333333333333333"),
            "category_stdev": Decimal("411.1724170922354859218446882"),
            "percent_above_mean": Decimal("1872.856778818094384756393290"),
        }
    },
}


def test_fingerprint_is_deterministic_across_runs():
    assert compute_fact_fingerprint(PAYLOAD) == compute_fact_fingerprint(deepcopy(PAYLOAD))


def test_fingerprint_changes_when_a_rule_value_changes():
    changed = deepcopy(PAYLOAD)
    changed["rules"]["amount_deviation"]["category_mean"] = Decimal("999.99")

    assert compute_fact_fingerprint(PAYLOAD) != compute_fact_fingerprint(changed)


def test_fingerprint_changes_when_the_transaction_snapshot_changes():
    changed = deepcopy(PAYLOAD)
    changed["transaction"]["amount"] = Decimal("1.00")

    assert compute_fact_fingerprint(PAYLOAD) != compute_fact_fingerprint(changed)


def test_fingerprint_changes_when_prompt_version_changes(monkeypatch):
    baseline = compute_fact_fingerprint(PAYLOAD)

    monkeypatch.setattr(fingerprint_module, "PROMPT_VERSION", "vX-test-only")

    assert compute_fact_fingerprint(PAYLOAD) != baseline


def test_fingerprint_excludes_evidence_by_construction():
    """SCRUM-53: tool evidence must never affect the fingerprint (see module
    docstring's staleness tradeoff) -- enforced structurally, by never
    accepting it as a parameter at all, rather than by convention/discipline
    at each call site."""
    assert list(inspect.signature(compute_fact_fingerprint).parameters) == ["payload"]
