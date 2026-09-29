"""SCRUM-53: compose_rationale + validate (app.investigation_agent.graph), the composition half
of the Investigation Agent pipeline that sits after collect_evidence. Mock mode only -- no
network, no real DB -- per the ticket's Phase A test list.

Most tests here call compose_rationale/validate directly against a hand-built InvestigationState
rather than running the full graph: composition/validation is what's under test, not tool
routing (already covered by test_investigation_agent_graph.py) or DB access, and calling the
nodes directly means these tests need no seeded database at all. get_chat_model is always
monkeypatched on app.investigation_agent.graph (where compose_rationale imports it), never left
to its real mock-mode default -- these tests are about what compose_rationale/validate do with a
*specific* model response, not about llm.py's own mock/live toggle (covered by
test_investigation_agent_llm.py).
"""

from datetime import datetime, timezone
from decimal import Decimal

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from app.investigation_agent import graph as graph_module
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


def _fake_model(text: str) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content=text)]))


def _fake_model_with_content(content: list) -> GenericFakeChatModel:
    """Like _fake_model, but for scripting a block-list `content` (e.g. a
    live model's extended-thinking response shape) instead of a plain
    string."""
    return GenericFakeChatModel(messages=iter([AIMessage(content=content)]))


def _compose_then_validate(state: InvestigationState) -> InvestigationState:
    composed = {**state, **compose_rationale(state)}
    validated = {**composed, **validate(composed)}
    return validated


class TestGroundedRationalePasses:
    def test_scripted_grounded_meridian_rationale_passes_with_agent_source(self, monkeypatch):
        scripted = (
            "This transaction was flagged for three reasons. It's your first purchase from "
            "Meridian Duty-Free Traders. The $3,200 amount is 1,873% higher than your typical "
            "spend in this category. It also occurred 6,750.82 miles from Seattle, WA, your "
            "typical location."
        )
        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _fake_model(scripted))

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

    def test_invented_number_fails(self, monkeypatch):
        scripted = "This transaction is $5,000 higher than your typical spend, which is unusual."
        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _fake_model(scripted))

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

    def test_wrong_unit_fails(self, monkeypatch):
        scripted = "This occurred 10,864 miles from Seattle, WA, your typical location."
        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _fake_model(scripted))

        result = _compose_then_validate(_meridian_state())

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        assert any(v.violation_type == "wrong_unit" for v in result["violations"])

    def test_bare_invented_place_fails(self, monkeypatch):
        scripted = "This looks closer to Tokyo than anywhere you usually shop."
        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _fake_model(scripted))

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

    def test_extracts_only_the_text_block_ignoring_a_thinking_block(self, monkeypatch):
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
        monkeypatch.setattr(
            graph_module, "get_chat_model", lambda: _fake_model_with_content(block_content)
        )

        result = _compose_then_validate(_meridian_state())

        assert "signature" not in result["rationale"]
        assert "thinking" not in result["rationale"]
        assert result["rationale_source"] == "agent"
        assert result["violations"] == []


class TestModelFailure:
    def test_model_raising_records_composition_error_and_produces_no_rationale(self, monkeypatch):
        class _RaisingModel:
            def invoke(self, *_args, **_kwargs):
                raise TimeoutError("simulated model timeout")

        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _RaisingModel())

        result = _compose_then_validate(_meridian_state())

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        assert result["composition_error"] == "simulated model timeout"
        assert any(v.violation_type == "empty_rationale" for v in result["violations"])

    def test_model_raising_does_not_crash_the_full_graph(self, monkeypatch):
        """Same failure, but through the full compiled graph (route_entry ->
        plan_tool_calls -> ... -> validate -> END) on the no-tool
        amount_deviation-only path, proving the graph completes end to end
        rather than propagating the exception."""

        class _RaisingModel:
            def invoke(self, *_args, **_kwargs):
                raise TimeoutError("simulated model timeout")

        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _RaisingModel())

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
        assert result["composition_error"] == "simulated model timeout"


class TestToolErrorCitationStillFails:
    def test_rationale_citing_a_failed_tools_facts_fails_validation(self, monkeypatch):
        """get_geo_distance failing (tool_errors, no evidence key) but the
        model still citing that tool's facts (distance_km, typical_location_label) must fail
        -- "no tool call, no citation" holds even though the model wasn't told the tool failed
        (it was only ever shown the facts that DID come back)."""
        scripted = "This occurred 10,864.40 km from Seattle, WA, your typical location."
        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _fake_model(scripted))

        state = _meridian_state(
            evidence={
                "get_merchant_risk_score": MERIDIAN_EVIDENCE["get_merchant_risk_score"],
            },
            tool_errors={"get_geo_distance": "simulated get_geo_distance failure"},
        )

        result = _compose_then_validate(state)

        assert result["rationale"] == ""
        assert result["rationale_source"] == "interim"
        assert any(v.violation_type in ("ungrounded_number", "unsupported_entity") for v in result["violations"])


class TestNoToolCompositionStillRuns:
    def test_amount_deviation_only_flag_still_composes_and_validates(self, monkeypatch):
        """No tool call is planned for an amount_deviation-only flag (SCRUM-51), but
        composition must still run on the no-tool path -- this exercises collect_evidence ->
        compose_rationale -> validate through the real compiled graph (not called directly),
        confirming the single documented path into composition (see build_graph's docstring)
        also covers the tool-less branch."""
        scripted = (
            "This purchase is $500 higher than your typical spend in this category, well above "
            "your usual pattern."
        )
        monkeypatch.setattr(graph_module, "get_chat_model", lambda: _fake_model(scripted))

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
