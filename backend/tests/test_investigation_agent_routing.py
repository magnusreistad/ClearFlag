"""SCRUM-54: routing and state transitions of the Investigation Agent graph,
end to end against seeded Postgres rows (tests/agent_fixtures.py) -- real
tool queries, scripted completions only. Complements
test_investigation_agent_graph.py, whose routing tests run against an empty
database and assert only which evidence keys appear.
"""

import itertools
import logging
import sys

import pytest
from agent_fixtures import (
    FLAGGED_SCENARIOS,
    completion,
    investigation_state,
    seed_scenario,
)
from langchain_core.messages import ToolMessage

from app.investigation_agent import graph as graph_module
from app.investigation_agent.graph import investigation_graph, plan_tool_calls
from app.models import AgentRationale
from app.rules.velocity import DEFAULT_WINDOW_MINUTES
from scripts import compose_rationales

# Stated here rather than imported from graph.RULE_TOOL_NODES, so these tests
# check that mapping instead of restating it.
EXPECTED_TOOL_FOR_RULE = {
    "velocity": "get_transaction_history",
    "new_merchant_risk": "get_merchant_risk_score",
    "geographic_anomaly": "get_geo_distance",
    "amount_deviation": None,
}
ALL_RULES = tuple(EXPECTED_TOOL_FOR_RULE)


def test_graph_structure_is_pinned():
    """Every node and edge of build_graph(), including both tools_condition
    branches -- a new node or a rewired edge fails here by name."""
    drawable = investigation_graph.get_graph()

    assert set(drawable.nodes) == {
        "__start__", "route_entry", "plan_tool_calls", "tools",
        "collect_evidence", "compose_rationale", "validate", "__end__",
    }
    assert {(e.source, e.target, e.conditional) for e in drawable.edges} == {
        ("__start__", "route_entry", False),
        ("route_entry", "plan_tool_calls", False),
        ("plan_tool_calls", "tools", True),
        ("plan_tool_calls", "collect_evidence", True),
        ("tools", "collect_evidence", False),
        ("collect_evidence", "compose_rationale", False),
        ("compose_rationale", "validate", False),
        ("validate", "__end__", False),
    }


@pytest.mark.parametrize(
    "rule_names",
    [list(c) for n in range(len(ALL_RULES) + 1) for c in itertools.combinations(ALL_RULES, n)],
    ids=lambda names: "+".join(names) or "none",
)
def test_every_rule_subset_plans_exactly_its_mapped_tools(rule_names):
    transaction = {
        "id": 7, "user_id": 3, "timestamp": None, "merchant": "Corner Grocer", "category": "groceries",
        "amount": None, "latitude": 1.0, "longitude": 2.0, "location_label": "Seattle, WA",
    }

    [message] = plan_tool_calls({"transaction": transaction, "rule_names": rule_names})["messages"]

    expected = {EXPECTED_TOOL_FOR_RULE[r] for r in rule_names} - {None}
    names = [call["name"] for call in message.tool_calls]
    assert sorted(names) == sorted(expected)  # one call per tool, no duplicates
    assert {call["id"] for call in message.tool_calls} == {f"{name}-7" for name in expected}


def _run(state):
    """Runs the compiled graph, returning (node names in execution order,
    final state)."""
    visited, final = [], None
    for mode, chunk in investigation_graph.stream(state, stream_mode=["updates", "values"]):
        if mode == "updates":
            visited.extend(chunk)
        else:
            final = chunk
    return visited, final


@pytest.mark.parametrize("name", FLAGGED_SCENARIOS)
def test_each_rule_type_routes_through_the_expected_nodes_and_tools(name, scripted_llm):
    seeded = seed_scenario(name)
    llm = scripted_llm(completion("grounded", seeded))

    visited, result = _run(investigation_state(seeded))

    tools = seeded.scenario.expected_tools
    assert visited == [
        "route_entry", "plan_tool_calls", *(["tools"] if tools else []),
        "collect_evidence", "compose_rationale", "validate",
    ]
    [plan, *tool_messages] = result["messages"]
    assert {call["name"] for call in plan.tool_calls} == tools
    assert all(isinstance(m, ToolMessage) and m.status == "success" for m in tool_messages)
    assert {m.name for m in tool_messages} == tools
    assert set(result["evidence"]) == tools
    assert result["tool_errors"] == {}
    assert llm.invocations == 1
    assert result["rationale_source"] == "agent"


@pytest.mark.parametrize("name", ["velocity", "new_merchant_risk", "geographic_anomaly", "meridian_triple"])
def test_tool_args_are_built_from_the_flagged_transaction(name, scripted_llm):
    seeded = seed_scenario(name)
    scripted_llm(completion("grounded", seeded))
    txn = seeded.primary

    _, result = _run(investigation_state(seeded))

    args_by_tool = {call["name"]: call["args"] for call in result["messages"][0].tool_calls}
    expected = {
        "get_transaction_history": {
            "user_id": txn["user_id"], "anchor_timestamp": txn["timestamp"], "window_minutes": DEFAULT_WINDOW_MINUTES,
        },
        "get_merchant_risk_score": {
            "user_id": txn["user_id"], "merchant": txn["merchant"], "anchor_timestamp": txn["timestamp"],
        },
        "get_geo_distance": {
            "user_id": txn["user_id"], "latitude": txn["latitude"], "longitude": txn["longitude"],
            "anchor_timestamp": txn["timestamp"],
        },
    }
    assert args_by_tool == {tool: expected[tool] for tool in seeded.scenario.expected_tools}


@pytest.mark.parametrize("name", ["velocity", "new_merchant_risk", "geographic_anomaly", "meridian_triple"])
def test_tool_evidence_is_real_and_agrees_with_the_rule(name, scripted_llm):
    """Against seeded history the tools return real figures (not the
    empty-history None/0 an unseeded database gives), and they agree with
    what the rule itself computed for the same transaction."""
    seeded = seed_scenario(name)
    scripted_llm(completion("grounded", seeded))
    rule_values = seeded.rule_values_by_id[seeded.primary_id]

    _, result = _run(investigation_state(seeded))
    evidence = result["evidence"]

    if "velocity" in rule_values:
        assert evidence["get_transaction_history"]["count"] == rule_values["velocity"]["transaction_count"] == 6
    if "new_merchant_risk" in rule_values:
        assert evidence["get_merchant_risk_score"]["is_first_transaction"] is True
        assert evidence["get_merchant_risk_score"]["prior_transaction_count"] == 0
    if "geographic_anomaly" in rule_values:
        geo = evidence["get_geo_distance"]
        assert geo["distance_miles"] == pytest.approx(rule_values["geographic_anomaly"]["distance_miles"])
        assert geo["typical_location_label"] == "Seattle, WA"


def test_zero_rule_user_never_invokes_the_agent(monkeypatch, scripted_llm, caplog):
    caplog.set_level(logging.INFO, logger="scripts.compose_rationales")
    seeded = seed_scenario("zero_rule")
    llm = scripted_llm()  # nothing scripted: any model call fails the test
    graph_invocations = []
    monkeypatch.setattr(graph_module, "invoke", lambda state: graph_invocations.append(state))
    monkeypatch.setattr(sys, "argv", ["compose_rationales", "--user-id", str(seeded.user_id)])

    compose_rationales.main()

    assert graph_invocations == []
    assert llm.invocations == 0
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        assert db.query(AgentRationale).count() == 0
    finally:
        db.close()
    assert "run complete: passed=0 validation_failed=0 composition_error=0 skipped_capped=0 skipped_already_passing=0" in caplog.text
