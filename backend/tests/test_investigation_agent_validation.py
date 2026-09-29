"""SCRUM-52: app.investigation_agent.validation, exercised against a static
fixture (tests/fixtures/investigation_agent_meridian_843.json) captured from
a real investigation_graph.invoke() run for transaction 843 (Meridian
Duty-Free Traders) -- no DB access, no LLM, no network in this file.
"""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

from app.investigation_agent.derived_facts import (
    compute_derived_facts,
    percent_above_category_mean,
)
from app.investigation_agent.prompts import MAX_RATIONALE_CHARS
from app.investigation_agent.validation import validate_rationale

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "investigation_agent_meridian_843.json"


def _load_fixture() -> tuple[dict, dict]:
    raw = json.loads(FIXTURE_PATH.read_text())
    payload = deepcopy(raw["payload"])
    payload["transaction"]["amount"] = Decimal(payload["transaction"]["amount"])
    amount_deviation = payload["rules"]["amount_deviation"]
    for key in ("amount", "category_mean", "category_stdev", "percent_above_mean"):
        amount_deviation[key] = Decimal(amount_deviation[key])
    new_merchant_risk = payload["rules"]["new_merchant_risk"]
    for key in ("amount", "typical_first_purchase_mean", "typical_first_purchase_stdev", "percent_above_mean"):
        new_merchant_risk[key] = Decimal(new_merchant_risk[key])
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


class TestBareSingleWordEntityGrounding:
    """SCRUM-53: the v1 hole documented in validation.py's module docstring
    -- a bare invented single capitalized word (no comma, no second
    capitalized word beside it) was never checked at all. These exercise the
    v2 heuristic that closes it (_check_entities' single-word pass).
    """

    def test_bare_invented_place_with_no_comma_fails(self, meridian):
        payload, evidence = meridian
        rationale = "This transaction occurred in Tokyo, which is unusual for this account."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "unsupported_entity"
        assert violation.span == "Tokyo"

    def test_bare_citable_single_word_from_a_multiword_fact_does_not_trigger_a_violation(self, meridian):
        """"Manila" alone is a real substring of the citable location_label
        "Manila, Philippines" -- it must ground on its own, not just when
        written with its full ", Philippines" suffix."""
        payload, evidence = meridian
        rationale = "This transaction occurred in Manila, which is unusual for this account."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True

    def test_sentence_initial_uncitable_word_now_fails_unless_a_common_word(self, meridian):
        """SCRUM-53 round 2/3: sentence-initial position is no longer a
        blanket pass on its own -- that was the reopened hole (a
        model-invented place name dropped at the start of a sentence used to
        sail through). This rationale's second sentence opens with an
        invented, uncitable word that isn't in the bundled NGSL common-word
        list either, so it must still be flagged despite being
        sentence-initial. ("Wanderlust" is a real English word, just not one
        of NGSL's ~2,800 core-vocabulary entries -- confirmed absent from
        the bundled list before writing this test.)"""
        payload, evidence = meridian
        rationale = "This is a routine notice. Wanderlust patterns like this are still being reviewed."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "unsupported_entity"
        assert violation.span == "Wanderlust"

    def test_bare_invented_entity_at_sentence_start_fails(self, meridian):
        """The exact regression this fix closes: an invented place name
        dropped at the very start of a sentence, with nothing forcing it
        into a citable "City, Region" or multi-word shape."""
        payload, evidence = meridian
        rationale = "Singapore is 9,000 miles from Seattle, WA, your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert any(v.violation_type == "unsupported_entity" and v.span == "Singapore" for v in result.violations)

    def test_bare_invented_entity_right_after_the_flagged_colon_prefix_fails(self, meridian):
        """The same regression, but immediately after the interim
        formatter's own "Flagged: " marker -- proving the marker is no
        longer treated as a generic "any colon exempts what follows" rule."""
        payload, evidence = meridian
        rationale = "Flagged: Singapore is 9,000 miles from Seattle, WA, your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert any(v.violation_type == "unsupported_entity" and v.span == "Singapore" for v in result.violations)

    @pytest.mark.parametrize(
        "invented_place",
        ["Singapore", "Tokyo", "Paris", "London", "China", "Lagos", "Bangkok"],
    )
    def test_invented_places_at_sentence_start_still_fail(self, meridian, invented_place):
        """SCRUM-53 round 3 regression guard: these are exactly the kind of
        word a stray proper noun in a "common words" list would wrongly
        exempt -- none of them are citable in the Meridian fixture, and none
        of them are in the bundled NGSL list (verified when NGSL was chosen,
        see data/NGSL_LICENSE.md), so all seven must still fail even at
        sentence-initial position, the position the dictionary check
        applies to."""
        payload, evidence = meridian
        rationale = f"{invented_place} is thousands of miles from Seattle, WA, your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert any(
            v.violation_type == "unsupported_entity" and v.span == invented_place for v in result.violations
        )

    @pytest.mark.parametrize(
        "starter",
        [
            "This", "It's", "You", "Your", "The", "A", "An", "These", "That", "We", "If",
            "However", "Additionally", "Because", "Together", "Overall",
        ],
    )
    def test_common_sentence_starters_are_not_flagged(self, meridian, starter):
        """SCRUM-53 round 3: sentence-initial exemption is now a dictionary
        check (_is_common_word against the bundled NGSL list), not a
        hand-picked allowlist -- this must pass for ANY common English word
        opening a sentence, not just ones someone enumerated. The first
        eleven are the old allowlist's exact entries (still expected to pass,
        now via the dictionary + its small grammatical-variant map); the
        last five ("However", "Additionally", "Because", "Together",
        "Overall") are natural sentence openers the old allowlist would have
        missed -- "Together" is the literal word that failed 839's real live
        composition and triggered this fix."""
        payload, evidence = meridian
        rationale = f"This is a routine notice. {starter} know that already."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True, result.violations

    def test_flagged_prefix_word_itself_is_never_flagged(self, meridian):
        """"Flagged" immediately followed by ':' is the interim formatter's
        own template keyword, not a proper noun -- it must never be checked
        for citability, independent of the common-word dictionary check."""
        payload, evidence = meridian
        rationale = "Flagged: This is your first purchase from Meridian Duty-Free Traders."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True

    def test_interim_formatters_flagged_colon_subject_prefix_is_not_flagged(self, meridian):
        """Every app.rules.* rationale reads "Flagged: <Subject> ..." with a
        different capitalized subject per rule ("This", "It's", "You") --
        _is_sentence_initial treats ':' as a clause boundary specifically so
        all of them are exempt, without enumerating every possible subject
        as a stopword. Exercises "You" here (velocity's own subject) since
        the fixture's other tests already exercise "This"."""
        payload, evidence = meridian
        rationale = "Flagged: You should note this occurred in Manila, Philippines, far from your usual pattern."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True

    def test_pronoun_i_is_never_flagged_even_mid_sentence(self, meridian):
        """"I" is the one common English word that's capitalized regardless
        of position, so it can't be exempted by the positional rule alone
        (unlike "Flagged: <Subject>" above) -- this places it genuinely
        mid-sentence (not sentence-initial) to actually exercise
        _SINGLE_WORD_STOPWORDS rather than the positional rule."""
        payload, evidence = meridian
        rationale = "Your purchase, I should note, occurred in Manila, Philippines, far from where you usually shop."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True


class TestScrum53Round3LiveRegression:
    """SCRUM-53 round 3: the false positive that actually happened. Live
    composition for transaction 839 (Precision Optics Co., $610.00 purchase,
    amount_deviation + new_merchant_risk) failed validation with a single
    violation: span="Together", violation_type="unsupported_entity" -- an
    ordinary word opening a sentence, not on the round-2 allowlist.

    IMPORTANT PROVENANCE NOTE: the fixture rationale below is a
    RECONSTRUCTION, not the verbatim model output. app.models.AgentRationale
    stores `rationale=None` on a failed validation by design (only a passing
    composition's text is persisted -- see models.py's docstring and
    scripts/compose_rationales.py's _compose_one), so the original failing
    text isn't recoverable from the audit trail; only the violation shape
    (that one span, that one violation_type) survived. This fixture
    reproduces that exact violation shape using txn 839's real, verified
    payload facts (queried live from the dev DB) with "Together" inserted at
    a sentence start -- every number below matches the values the actual
    passing retry (agent_rationales id=12) used and validated successfully,
    so the only violation this fixture should produce is the "Together" one.
    """

    PAYLOAD_839: ClassVar[dict] = {
        "transaction": {
            "id": 839,
            "user_id": 2,
            "merchant": "Precision Optics Co.",
            "category": "shopping",
            "amount": Decimal("610.00"),
            "location_label": "Seattle, WA",
            "latitude": 47.63484123358453,
            "longitude": -122.33133774111707,
        },
        "rule_names": ["amount_deviation", "new_merchant_risk"],
        "rules": {
            "amount_deviation": {
                "amount": Decimal("610.00"),
                "category_mean": Decimal("53.01222222222222222222222222"),
                "category_stdev": Decimal("32.07635608114557086627671755"),
                "percent_above_mean": Decimal("1050.678040703401731256942843"),
            },
            "new_merchant_risk": {
                "amount": Decimal("610.00"),
                "typical_first_purchase_mean": Decimal("38.64379310344827586206896552"),
                "typical_first_purchase_stdev": Decimal("29.67773894133363285313806290"),
                "percent_above_mean": Decimal("1478.519992504483924794988712"),
            },
        },
    }

    def test_reconstructed_839_rationale_now_passes(self):
        rationale = (
            "This $610.00 purchase at Precision Optics Co. is 1051% above your typical shopping "
            "spend of $53. Together, this amount and your lack of any history with the merchant "
            "made it 1479% above the $39 you typically spend on a first purchase with a new "
            "merchant."
        )

        result = validate_rationale(rationale, self.PAYLOAD_839, evidence={})

        assert result.passed is True, result.violations

    def test_together_alone_was_the_only_thing_that_failed(self):
        """Confirms the fixture is otherwise fully grounded: reverting just
        the fix (by checking against the OLD round-2 allowlist behavior
        would be redundant to re-implement here) isn't needed -- instead,
        this confirms the same text minus the "Together" sentence produces
        zero violations, isolating that word as the only thing this fixture
        was ever testing."""
        rationale_without_together_sentence = (
            "This $610.00 purchase at Precision Optics Co. is 1051% above your typical shopping "
            "spend of $53. It's also 1479% above the $39 you typically spend on a first purchase "
            "with a new merchant."
        )

        result = validate_rationale(rationale_without_together_sentence, self.PAYLOAD_839, evidence={})

        assert result.passed is True, result.violations


class TestInflectedFormsAtSentenceStart:
    """SCRUM-53 follow-up: NGSL is a lemma list, so an ordinary INFLECTED
    word opening a sentence -- "Purchases", "Looking", "Based" -- failed
    before _is_common_word learned to strip regular inflections back to a
    lemma (_regular_inflection_candidates). "Transactions" is the control:
    its lemma "transaction" isn't in NGSL at all (a domain/financial term
    outside this general vocabulary list), so it still fails no matter what
    inflection-stripping does -- a genuine dictionary gap, not a stripping
    bug, and worth keeping as its own case so a future change to the
    stripping logic can't accidentally make it look fixed.
    """

    @pytest.mark.parametrize("word", ["Purchases", "Looking", "Based"])
    def test_regular_inflections_of_an_ngsl_lemma_now_pass(self, meridian, word):
        """Each of these has a real NGSL lemma ("purchase", "look", "base")
        -- confirmed present in the bundled list -- so stripping the regular
        plural/-ing/-ed inflection should recover it."""
        payload, evidence = meridian
        rationale = f"This is a routine notice. {word} something unrelated to this account."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True, result.violations

    def test_transactions_still_fails_since_its_lemma_isnt_in_ngsl(self, meridian):
        """The control case: "transaction" is a domain/financial term, not
        part of NGSL's general ~2,800-word pedagogical vocabulary -- no
        amount of inflection-stripping can recover a lemma that was never in
        the dictionary to begin with. Documented, not a bug."""
        payload, evidence = meridian
        rationale = "This is a routine notice. Transactions something unrelated to this account."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert any(v.span == "Transactions" for v in result.violations)

    @pytest.mark.parametrize("invented_place_form", ["Tokyo's", "Londons"])
    def test_inflected_or_possessive_place_names_still_fail(self, meridian, invented_place_form):
        """Guards the fix from reopening the proper-noun hole via an
        inflected/possessive form: "Tokyo's" is handled by the apostrophe-
        stem check (stem "tokyo" absent from NGSL); "Londons" hits the
        plain -s inflection candidate ("london", also absent) -- both must
        still fail exactly like their bare forms do."""
        payload, evidence = meridian
        rationale = f"{invented_place_form} was very far from Seattle, WA, your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert any(v.span == invented_place_form for v in result.violations)


class TestErroredToolNotCitable:
    """SCRUM-68 update: distance_miles is no longer a tool-only fact --
    app.rules.geographic_anomaly.RuleHit.values now exposes the same
    great-circle figure get_geo_distance computes, both legitimate citation
    sources per the Design Doc (payload OR tool output). These tests now
    exercise distance_km and typical_location_label instead -- the two
    get_geo_distance fields this rule genuinely never computes -- to prove
    "no tool call, no citation" still holds for facts only a tool produces.
    """

    def test_tool_only_numeric_value_fails(self, meridian):
        payload, evidence = meridian
        del evidence["get_geo_distance"]  # simulates get_geo_distance raising (SCRUM-51 tool_errors)
        rationale = "This occurred 10,864.40 km from your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert _violation_types(result) == ["ungrounded_number"]
        assert result.violations[0].span == "10,864.40 km"

    def test_tool_only_entity_fails(self, meridian):
        payload, evidence = meridian
        del evidence["get_geo_distance"]  # simulates get_geo_distance raising (SCRUM-51 tool_errors)
        rationale = "This is far from Seattle, WA, your typical location."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert _violation_types(result) == ["unsupported_entity"]
        assert result.violations[0].span == "Seattle, WA"

    def test_distance_miles_still_grounds_when_the_tool_fails_since_the_rule_also_computes_it(self, meridian):
        """Confirms the SCRUM-68 restoration deliberately: distance_miles is
        NOT tool-only anymore, so it stays citable even with get_geo_distance
        evidence removed -- this is the intended behavior change, not a gap.
        """
        payload, evidence = meridian
        del evidence["get_geo_distance"]
        rationale = "This occurred 6,750.82 miles from where you usually shop."

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True


class TestEmptyRationale:
    @pytest.mark.parametrize("rationale", ["", "   ", "\n\t"])
    def test_empty_or_whitespace_only_rationale_fails(self, meridian, rationale):
        payload, evidence = meridian

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        [violation] = result.violations
        assert violation.violation_type == "empty_rationale"


class TestLengthCap:
    """SCRUM-53: the ~500-char MAX_RATIONALE_CHARS cap prompts.py already
    communicates to the model is now actually enforced here -- never by
    truncating (see validate_rationale's docstring), only by failing
    validation so the caller falls back to the interim formatter.
    """

    # A fully-grounded rationale, padded with plain lowercase filler (no
    # capital letters, no digits) so padding can never itself introduce an
    # unrelated numeric/entity violation -- these tests are isolating the
    # length check alone.
    _GROUNDED_BASE = (
        "This transaction was flagged for one reason. Your purchase from "
        "Meridian Duty-Free Traders was far from Seattle, WA."
    )
    _FILLER = " the amount and location noted above were as expected for this account"

    @classmethod
    def _rationale_of_length(cls, length: int) -> str:
        padded = cls._GROUNDED_BASE + cls._FILLER * (length // len(cls._FILLER) + 1)
        return padded[:length]

    def test_rationale_exactly_at_cap_passes(self, meridian):
        payload, evidence = meridian
        rationale = self._rationale_of_length(MAX_RATIONALE_CHARS)
        assert len(rationale) == MAX_RATIONALE_CHARS

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is True, result.violations

    def test_rationale_one_over_the_cap_fails(self, meridian):
        payload, evidence = meridian
        rationale = self._rationale_of_length(MAX_RATIONALE_CHARS + 1)
        assert len(rationale) == MAX_RATIONALE_CHARS + 1

        result = validate_rationale(rationale, payload, evidence)

        assert result.passed is False
        assert any(v.violation_type == "rationale_too_long" for v in result.violations)

    def test_rationale_over_cap_is_never_truncated(self, meridian):
        """The violation records the actual length, not a truncated one --
        confirming the check runs against the real rationale, not a
        silently-shortened copy of it."""
        payload, evidence = meridian
        rationale = self._rationale_of_length(MAX_RATIONALE_CHARS + 50)

        result = validate_rationale(rationale, payload, evidence)

        [length_violation] = [v for v in result.violations if v.violation_type == "rationale_too_long"]
        assert str(MAX_RATIONALE_CHARS + 50) in length_violation.reason


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
