"""SCRUM-53: compose_rationale + validate (app.investigation_agent.graph), the composition half
of the Investigation Agent pipeline that sits after collect_evidence. Mock mode only -- no
network, no real DB -- per the ticket's Phase A test list.

Most tests here call compose_rationale/validate directly against a hand-built InvestigationState
rather than running the full graph: composition/validation is what's under test, not tool
routing (already covered by test_investigation_agent_graph.py) or DB access, and calling the
nodes directly means these tests need no seeded database at all. Every model response
(including a raised exception) is scripted through the scripted_llm fixture (SCRUM-54,
tests/agent_fixtures.py's ScriptedLLM), never left to get_chat_model's canned mock default --
these tests are about what compose_rationale/validate do with a *specific* model response, not
about llm.py's own mock/live toggle (covered by test_investigation_agent_llm.py).
"""

from datetime import datetime, timezone
from decimal import Decimal

from agent_fixtures import anthropic_error
from langchain_core.messages import AIMessage, ToolMessage

from app.investigation_agent.graph import (
    collect_evidence,
    compose_rationale,
    investigation_graph,
    validate,
)
from app.investigation_agent.state import InvestigationState

MERIDIAN_TRANSACTION = {
    "id": 843,
    "user_id": 2,
    "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
    "merchant": "Meridian Duty-Free Traders",
    "category": "shopping",
    "amount": Decimal("3200.00"),
    "latitude": 14.5995,
    "longitude": 120.9842,
    "location_label": "Manila, Philippines",
}

MERIDIAN_RULE_VALUES = {
    "geographic_anomaly": {
        "location_label": "Manila, Philippines",
        "distance_miles": 6750.823956517019,
        "distance_mean_miles": 209.35920399831326,
        "distance_stdev_miles": 793.7444239056688,
    },
    "amount_deviation": {
        "amount": Decimal("3200.00"),
        "category_mean": Decimal("162.2013333333333333333333333"),
        "category_stdev": Decimal("411.1724170922354859218446882"),
        "percent_above_mean": Decimal("1872.856778818094384756393290"),
    },
    "new_merchant_risk": {
        "amount": Decimal("3200.00"),
        "typical_first_purchase_mean": Decimal("107.8462745098039215686274510"),
        "typical_first_purchase_stdev": Decimal("322.9325560420366431762533125"),
        "percent_above_mean": Decimal("2867.186409122643704910402606"),
    },
}

MERIDIAN_EVIDENCE = {
    "get_geo_distance": {
        "user_id": 2,
        "latitude": 14.5995,
        "longitude": 120.9842,
        "distance_km": 10864.398029476926,
        "distance_miles": 6750.823956517019,
        "typical_location_label": "Seattle, WA",
    },
    "get_merchant_risk_score": {
        "user_id": 2,
        "merchant": "Meridian Duty-Free Traders",
        "is_first_transaction": True,
        "prior_transaction_count": 0,
        "risk_tier": None,
    },
}


def _meridian_state(**overrides) -> InvestigationState:
    state: InvestigationState = {
        "transaction": MERIDIAN_TRANSACTION,
        "rule_names": ["geographic_anomaly", "amount_deviation", "new_merchant_risk"],
        "rule_values": MERIDIAN_RULE_VALUES,
        "evidence": MERIDIAN_EVIDENCE,
        "tool_errors": {},
        "rationale": "",
    }
    state.update(overrides)
    return state


def _compose_then_validate(state: InvestigationState) -> InvestigationState:
    composed = {**state, **compose_rationale(state)}
    validated = {**composed, **validate(composed)}
    return validated


class TestGroundedRationalePasses:
    def test_scripted_grounded_meridian_rationale_passes_with_agent_source(self, scripted_llm):
        scripted = (
            "This transaction was flagged for three reasons. It's your first purchase from "
            "Meridian Duty-Free Traders. The $3,200 amount is 1,873% higher than your typical "
            "spend in this category. It also occurred 6,750.82 miles from Seattle, WA, your "
            "typical location."
        )
        scripted_llm(AIMessage(content=scripted))

        result = _compose_then_validate(_meridian_state())

        assert result["rationale"] == scripted
        assert result["rationale_source"] == "agent"
        assert result["violations"] == []
        assert result["composition_error"] is None


class TestUngroundedRationaleFails:
    """Each of these scripts a plausible-looking but ungrounded rationale --
    every one must fail validation, discard the composed text, and fall back
    to rationale_source="interim" rather than being repaired or partially
    accepted (Design Doc: never fabricate, never repair)."""

    def test_invented_number_fails(self, scripted_llm):
        scripted = "This transaction is $5,000 higher than your typical spend, which is unusual."
        scripted_llm(AIMessage(content=scripted))

        result = _compose_then_validate(_meridian_state())

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        assert any(v.violation_type == "ungrounded_number" for v in result["violations"])
        # SCRUM-53 follow-up: composed_rationale is compose_rationale's own
        # audit record and validate() never touches it -- unlike `rationale`
        # above, it must still hold the actual (ungrounded) model output
        # even after validation discards it, so a caller (scripts.compose_
        # rationales) can persist what was really produced on a failure.
        assert result["composed_rationale"] == scripted

    def test_wrong_unit_fails(self, scripted_llm):
        scripted = "This occurred 10,864 miles from Seattle, WA, your typical location."
        scripted_llm(AIMessage(content=scripted))

        result = _compose_then_validate(_meridian_state())

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        assert any(v.violation_type == "wrong_unit" for v in result["violations"])

    def test_bare_invented_place_fails(self, scripted_llm):
        scripted = "This looks closer to Tokyo than anywhere you usually shop."
        scripted_llm(AIMessage(content=scripted))

        result = _compose_then_validate(_meridian_state())

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        assert any(v.violation_type == "unsupported_entity" and v.span == "Tokyo" for v in result["violations"])


class TestBlockContentResponses:
    """SCRUM-53 Phase A live check: a live model can return `content` as a
    list of content blocks (e.g. an extended-thinking block alongside the
    text block) rather than a plain string -- observed live for the
    three-rule Meridian transaction. `str(response.content)` on that list
    used to stringify the whole list (thinking block and its base64
    signature included) as the "rationale"; compose_rationale now reads
    `response.text` instead, which extracts just the text-type block(s)."""

    def test_extracts_only_the_text_block_ignoring_a_thinking_block(self, scripted_llm):
        block_content = [
            {"type": "thinking", "thinking": "internal reasoning", "signature": "abc123"},
            {
                "type": "text",
                "text": (
                    "This transaction was flagged for three reasons. It's your first purchase "
                    "from Meridian Duty-Free Traders. The $3,200 amount is 1,873% higher than "
                    "your typical spend in this category. It also occurred 6,750.82 miles from "
                    "Seattle, WA, your typical location."
                ),
            },
        ]
        scripted_llm(AIMessage(content=block_content))

        result = _compose_then_validate(_meridian_state())

        assert "signature" not in result["rationale"]
        assert "thinking" not in result["rationale"]
        assert result["rationale_source"] == "agent"
        assert result["violations"] == []


class TestModelFailure:
    def test_model_raising_records_composition_error_and_produces_no_rationale(self, scripted_llm):
        scripted_llm(TimeoutError("simulated model timeout"))

        result = _compose_then_validate(_meridian_state())

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        # SCRUM-56: "ModelError: <ExceptionClass>(<status_code or ->): <message>" --
        # TimeoutError carries no status_code, so getattr(exc, "status_code", None) is None.
        assert result["composition_error"] == "ModelError: TimeoutError(-): simulated model timeout"
        assert result["composition_error_label"] == "ModelError:TimeoutError(-)"
        # SCRUM-56: validate() skips its grounding checks on a composition_error --
        # an empty `rationale` here is a structural consequence of the model call
        # failing, not a citation problem, so it must not be recorded as one.
        assert result["violations"] == []

    def test_model_raising_does_not_crash_the_full_graph(self, scripted_llm):
        """Same failure, but through the full compiled graph (route_entry ->
        plan_tool_calls -> ... -> validate -> END) on the no-tool
        amount_deviation-only path, proving the graph completes end to end
        rather than propagating the exception."""

        scripted_llm(TimeoutError("simulated model timeout"))

        state: InvestigationState = {
            "transaction": {
                "id": 2,
                "user_id": 1,
                "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "merchant": "Test Merchant",
                "category": "shopping",
                "amount": Decimal("500.00"),
                "latitude": 47.6062,
                "longitude": -122.3321,
                "location_label": "Seattle, WA",
            },
            "rule_names": ["amount_deviation"],
            "rule_values": {
                "amount_deviation": {
                    "amount": Decimal("500.00"),
                    "category_mean": Decimal("100.00"),
                    "category_stdev": Decimal("10.00"),
                    "percent_above_mean": Decimal("400.00"),
                }
            },
            "evidence": {},
            "rationale": "",
        }

        result = investigation_graph.invoke(state)

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        assert result["composition_error"] == "ModelError: TimeoutError(-): simulated model timeout"
        assert result["violations"] == []

    def test_model_raising_with_a_status_code_records_it(self, scripted_llm):
        """A real anthropic API error (e.g. a 500/503 surviving SDK
        max_retries) carries a status_code attribute -- confirms
        getattr(exc, "status_code", None) picks it up rather than always
        falling back to "-"."""

        class _StatusCodedError(Exception):
            def __init__(self, message: str, status_code: int):
                super().__init__(message)
                self.status_code = status_code

        scripted_llm(_StatusCodedError("credential validation failed", status_code=500))

        result = _compose_then_validate(_meridian_state())

        assert result["composition_error"] == "ModelError: _StatusCodedError(500): credential validation failed"
        assert result["composition_error_label"] == "ModelError:_StatusCodedError(500)"
        assert result["violations"] == []

    def test_a_real_anthropic_api_timeout_error_is_recorded_as_such(self, scripted_llm):
        """SCRUM-56 ticket input #1/#3: once get_chat_model()'s own SDK
        max_retries are exhausted, the exception that reaches compose_rationale
        can be a real anthropic SDK exception class -- confirms the real
        anthropic.APITimeoutError (raised on a request timeout, carries no
        status_code) round-trips through the generic `except Exception`
        handling the same as any other exception, by class name."""

        scripted_llm(anthropic_error("timeout"))

        result = _compose_then_validate(_meridian_state())

        assert result["composition_error"].startswith("ModelError: APITimeoutError(-): ")
        assert result["composition_error_label"] == "ModelError:APITimeoutError(-)"
        assert result["violations"] == []

    def test_model_raising_a_long_message_is_truncated(self, scripted_llm):
        long_message = "x" * 1000

        scripted_llm(RuntimeError(long_message))

        result = _compose_then_validate(_meridian_state())

        assert len(result["composition_error"]) < len(long_message)
        assert result["composition_error"].startswith("ModelError: RuntimeError(-): " + "x" * 20)


class TestToolErrorAbortsComposition:
    """SCRUM-56: a non-empty tool_errors now short-circuits compose_rationale
    entirely -- no model call, no partial-evidence composition -- replacing
    SCRUM-51's original "citing a failed tool's facts still fails
    validation" behavior (which required a model call to happen first)."""

    def test_tool_error_means_no_model_call_and_a_tool_error_composition_error(self, scripted_llm):
        # Nothing scripted: scripted_llm fails the test on any model call.
        llm = scripted_llm()

        # The exact shape ToolNode(handle_tool_errors=True) produces:
        # "Error: <repr(exc)>\n Please fix your mistakes." (TOOL_CALL_ERROR_TEMPLATE).
        state = _meridian_state(
            evidence={
                "get_merchant_risk_score": MERIDIAN_EVIDENCE["get_merchant_risk_score"],
            },
            tool_errors={
                "get_geo_distance": "Error: RuntimeError('simulated get_geo_distance failure')\n Please fix your mistakes."
            },
        )

        result = _compose_then_validate(state)

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        assert result["violations"] == []
        assert result["composition_error"] == (
            "ToolError[get_geo_distance]: RuntimeError: simulated get_geo_distance failure"
        )
        assert result["composition_error_label"] == "ToolError[get_geo_distance]:RuntimeError"
        assert llm.invocations == 0

    def test_multiple_tool_errors_are_all_named(self, scripted_llm):
        # Nothing scripted: scripted_llm fails the test on any model call.
        llm = scripted_llm()

        state = _meridian_state(
            evidence={},
            tool_errors={
                "get_geo_distance": "Error: RuntimeError('boom')\n Please fix your mistakes.",
                "get_merchant_risk_score": "Error: ValueError('also boom')\n Please fix your mistakes.",
            },
        )

        result = _compose_then_validate(state)

        assert "ToolError[get_geo_distance]: RuntimeError: boom" in result["composition_error"]
        assert "ToolError[get_merchant_risk_score]: ValueError: also boom" in result["composition_error"]
        assert result["violations"] == []
        assert llm.invocations == 0


class TestNoToolCompositionStillRuns:
    def test_amount_deviation_only_flag_still_composes_and_validates(self, scripted_llm):
        """No tool call is planned for an amount_deviation-only flag (SCRUM-51), but
        composition must still run on the no-tool path -- this exercises collect_evidence ->
        compose_rationale -> validate through the real compiled graph (not called directly),
        confirming the single documented path into composition (see build_graph's docstring)
        also covers the tool-less branch."""
        scripted = (
            "This purchase is $500 higher than your typical spend in this category, well above "
            "your usual pattern."
        )
        scripted_llm(AIMessage(content=scripted))

        state: InvestigationState = {
            "transaction": {
                "id": 2,
                "user_id": 1,
                "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "merchant": "Test Merchant",
                "category": "shopping",
                "amount": Decimal("500.00"),
                "latitude": 47.6062,
                "longitude": -122.3321,
                "location_label": "Seattle, WA",
            },
            "rule_names": ["amount_deviation"],
            "rule_values": {
                "amount_deviation": {
                    "amount": Decimal("500.00"),
                    "category_mean": Decimal("100.00"),
                    "category_stdev": Decimal("10.00"),
                    "percent_above_mean": Decimal("400.00"),
                }
            },
            "evidence": {},
            "rationale": "",
        }

        result = investigation_graph.invoke(state)

        assert result["evidence"] == {}  # no tool ever ran
        assert result["rationale"] == scripted
        assert result["rationale_source"] == "agent"
        assert result["violations"] == []


class TestCollectEvidenceUnchangedMapping:
    """SCRUM-53: collect_evidence's own mapping logic, moved out of the old assemble_rationale
    unchanged (see graph.py's docstring) -- adapted from the SCRUM-51 tests that used to exercise
    this via assemble_rationale directly. Confirms collect_evidence no longer writes a
    `rationale` key at all (that's compose_rationale's job now)."""

    def test_successful_tool_messages_populate_evidence_keyed_by_tool_name(self):
        state: InvestigationState = {
            "transaction": MERIDIAN_TRANSACTION,
            "rule_names": [],
            "messages": [
                ToolMessage(
                    content="ok",
                    name="get_geo_distance",
                    tool_call_id="get_geo_distance-843",
                    artifact={"distance_km": 100.0},
                ),
                ToolMessage(
                    content="ok",
                    name="get_merchant_risk_score",
                    tool_call_id="get_merchant_risk_score-843",
                    artifact={"is_first_transaction": True},
                ),
            ],
            "evidence": {},
            "rationale": "",
        }

        result = collect_evidence(state)

        assert result == {
            "evidence": {
                "get_geo_distance": {"distance_km": 100.0},
                "get_merchant_risk_score": {"is_first_transaction": True},
            },
            "tool_errors": {},
        }
        assert "rationale" not in result

    def test_error_tool_messages_go_to_tool_errors_not_evidence(self):
        state: InvestigationState = {
            "transaction": MERIDIAN_TRANSACTION,
            "rule_names": [],
            "messages": [
                ToolMessage(
                    content="simulated failure",
                    name="get_geo_distance",
                    tool_call_id="get_geo_distance-843",
                    status="error",
                ),
                ToolMessage(
                    content="ok",
                    name="get_merchant_risk_score",
                    tool_call_id="get_merchant_risk_score-843",
                    artifact={"is_first_transaction": True},
                ),
            ],
            "evidence": {},
            "rationale": "",
        }

        result = collect_evidence(state)

        assert result["evidence"] == {"get_merchant_risk_score": {"is_first_transaction": True}}
        assert result["tool_errors"] == {"get_geo_distance": "simulated failure"}

    def test_no_tool_messages_at_all_returns_empty_dicts(self):
        state: InvestigationState = {
            "transaction": MERIDIAN_TRANSACTION,
            "rule_names": ["amount_deviation"],
            "messages": [AIMessage(content="", tool_calls=[])],
            "evidence": {},
            "rationale": "",
        }

        result = collect_evidence(state)

        assert result == {"evidence": {}, "tool_errors": {}}
