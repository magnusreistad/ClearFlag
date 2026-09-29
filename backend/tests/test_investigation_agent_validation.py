"""SCRUM-52: app.investigation_agent.validation, exercised against a static
fixture (tests/fixtures/investigation_agent_meridian_843.json) captured from
a real investigation_graph.invoke() run for transaction 843 (Meridian
Duty-Free Traders) -- no DB access, no LLM, no network in this file.
"""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from app.investigation_agent.derived_facts import (
    compute_derived_facts,
    percent_above_category_mean,
)
from app.investigation_agent.validation import validate_rationale

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "investigation_agent_meridian_843.json"


def _load_fixture() -> tuple[dict, dict]:
    raw = json.loads(FIXTURE_PATH.read_text())
    payload = deepcopy(raw["payload"])
    payload["transaction"]["amount"] = Decimal(payload["transaction"]["amount"])
    amount_deviation = payload["rules"]["amount_deviation"]
    amount_deviation["amount"] = Decimal(amount_deviation["amount"])
    amount_deviation["category_mean"] = Decimal(amount_deviation["category_mean"])
    amount_deviation["category_stdev"] = Decimal(amount_deviation["category_stdev"])
    evidence = deepcopy(raw["evidence"])
    return payload, evidence


@pytest.fixture
def meridian():
    return _load_fixture()


def _violation_types(result):
    return [v.violation_type for v in result.violations]


class TestPass:
    def test_realistic_multi_rule_rationale_passes(self, meridian):
        payload, evidence = meridian
        rationale = (
            "This transaction was flagged for three reasons. It's your first "
            "purchase from Meridian Duty-Free Traders. The $3,200 amount is "
            "1,873% higher than your typical spend in this category. It also "
            "occurred 6,750.82 miles from Seattle, WA, your typical location."
        )

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True
        assert result.violations == []


class TestNumericGrounding:
    def test_invented_number_fails(self, meridian):
        payload, evidence = meridian
        rationale = "The amount is $5,000, which is far above your usual spend."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "ungrounded_number"
        assert violation.span == "$5,000"

    def test_wrong_unit_fails_even_though_the_number_exists_as_km(self, meridian):
        payload, evidence = meridian
        rationale = "This occurred 10,864 miles from Seattle, WA, your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "wrong_unit"
        assert violation.span == "10,864 miles"

    def test_truncated_number_fails(self, meridian):
        payload, evidence = meridian
        rationale = "This occurred 6,750 miles from Seattle, WA, your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "truncated_number"
        assert violation.span == "6,750 miles"

    def test_correctly_rounded_number_passes(self, meridian):
        payload, evidence = meridian
        rationale = "This occurred 6,751 miles from Seattle, WA, your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True


class TestNamedEntityGrounding:
    def test_invented_location_fails(self, meridian):
        payload, evidence = meridian
        rationale = "This looks closer to Tokyo, Japan than anywhere you usually shop."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "unsupported_entity"
        assert violation.span == "Tokyo, Japan"

    def test_invented_merchant_fails(self, meridian):
        payload, evidence = meridian
        rationale = "This is similar to your prior purchase at Aurora Fine Jewelers."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "unsupported_entity"
        assert violation.span == "Aurora Fine Jewelers"

    def test_citable_merchant_and_location_do_not_trigger_a_violation(self, meridian):
        payload, evidence = meridian
        rationale = "Your purchase from Meridian Duty-Free Traders was far from Seattle, WA."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True


class TestErroredToolNotCitable:
    def test_value_from_errored_tool_fails(self, meridian):
        payload, evidence = meridian
        del evidence["get_geo_distance"]  # simulates get_geo_distance raising (SCRUM-51 tool_errors)
        rationale = (
            "This is your first purchase from Meridian Duty-Free Traders, and "
            "it occurred 6,750.82 miles from your typical location."
        )

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert _violation_types(result) == ["ungrounded_number"]
        assert result.violations[0].span == "6,750.82 miles"


class TestEmptyRationale:
    @pytest.mark.parametrize("rationale", ["", "   ", "\n\t"])
    def test_empty_or_whitespace_only_rationale_fails(self, meridian, rationale):
        payload, evidence = meridian

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "empty_rationale"


class TestDerivedFacts:
    def test_percent_above_category_mean_matches_interim_formatters_formula(self, meridian):
        payload, _evidence = meridian
        amount = payload["rules"]["amount_deviation"]["amount"]
        category_mean = payload["rules"]["amount_deviation"]["category_mean"]

        # The exact formula app.rules.amount_deviation.evaluate_amount_deviation
        # uses for `pct_higher`, reproduced independently here so this test
        # can't drift silently alongside a bug in derived_facts.py.
        expected = (amount - category_mean) / category_mean * 100

        assert percent_above_category_mean(amount, category_mean) == expected

    def test_compute_derived_facts_includes_amount_deviation_percent(self, meridian):
        payload, _evidence = meridian

        derived = compute_derived_facts(payload)

        assert round(derived["amount_deviation"]["percent_above_mean"]) == 1873

    def test_compute_derived_facts_omits_a_rule_with_no_payload_data(self):
        derived = compute_derived_facts({"transaction": {}, "rule_names": [], "rules": {}})

        assert derived == {}
