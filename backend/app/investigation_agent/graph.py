"""Investigation Agent StateGraph skeleton (SCRUM-47).

Routes evidence-gathering tool calls based on which rule(s) flagged a
transaction. Pure scaffolding: the router and fan-out edges are real, but
the three tool nodes below return dummy/passthrough evidence -- SCRUM-48/49/50
drop in the real get_transaction_history / get_merchant_risk_score /
get_geo_distance logic without touching this file's node/edge structure.

Not wired into the live FastAPI request path yet (that's SCRUM-53). The
interim formatter in app.rules.engine (concatenate_rationales) stays the
`rationale` field's source until then, and remains in the codebase
afterward as the on-failure fallback path (SCRUM-56).
"""

from typing import Literal

from langgraph.graph import END, StateGraph

from app.investigation_agent.state import InvestigationState

TOOL_NODE_NAMES = ("get_transaction_history", "get_merchant_risk_score", "get_geo_distance")

# SCRUM-51: which tool each rule's evidence comes from. amount_deviation has
# no entry -- per SCRUM-51, its values (mean, stdev, actual amount) are
# already computed by the rules engine and passed in the invocation payload,
# so it needs no extra evidence-gathering tool call.
RULE_TOOL_NODES: dict[str, str] = {
    "velocity": "get_transaction_history",
    "new_merchant_risk": "get_merchant_risk_score",
    "geographic_anomaly": "get_geo_distance",
}


def route_entry(state: InvestigationState) -> dict:
    """Entry node. No-op passthrough -- exists as the fixed anchor the
    conditional fan-out edge (route_to_tools) hangs off of."""
    return {}


def route_to_tools(
    state: InvestigationState,
) -> list[Literal["get_transaction_history", "get_merchant_risk_score", "get_geo_distance", "assemble_rationale"]]:
    """Reads triggered rule names from state and returns every tool node
    that has evidence to gather for them.

    Returns a list, not a single node name: LangGraph runs every entry as a
    parallel branch within the same superstep. That's a deliberate choice
    here (confirmed against SCRUM-47), not a default -- the three tools are
    independent evidence lookups for different rules, so nothing requires
    one branch's output before another can run. If no triggered rule maps
    to a tool (e.g. an amount_deviation-only flag), routes straight to
    assemble_rationale so every investigation still terminates.
    """
    targets = [RULE_TOOL_NODES[rule] for rule in state["rule_names"] if rule in RULE_TOOL_NODES]
    return targets or ["assemble_rationale"]


def get_transaction_history(state: InvestigationState) -> dict:
    """Placeholder for SCRUM-48.

    Real inputs (per SCRUM-48): user_id, window_minutes.
    Real output: list of transactions (timestamp, amount, merchant) within
    the window + a count. Triggered by the velocity rule.
    """
    transaction = state["transaction"]
    return {
        "evidence": {
            "get_transaction_history": {
                "_placeholder": True,
                "user_id": transaction["user_id"],
                "transactions": [],
                "count": 0,
            }
        }
    }


def get_merchant_risk_score(state: InvestigationState) -> dict:
    """Placeholder for SCRUM-49.

    Real inputs (per SCRUM-49): user_id, merchant_id.
    Real output: is_first_transaction, prior_transaction_count, risk_tier.
    Triggered by the new_merchant_risk rule.
    """
    transaction = state["transaction"]
    return {
        "evidence": {
            "get_merchant_risk_score": {
                "_placeholder": True,
                "user_id": transaction["user_id"],
                "merchant": transaction["merchant"],
                "is_first_transaction": None,
                "prior_transaction_count": None,
                "risk_tier": None,
            }
        }
    }


def get_geo_distance(state: InvestigationState) -> dict:
    """Placeholder for SCRUM-50.

    Real inputs (per SCRUM-50): user_id, transaction_lat, transaction_long.
    Real output: distance_km, typical_location_label. Triggered by the
    geographic_anomaly rule.
    """
    transaction = state["transaction"]
    return {
        "evidence": {
            "get_geo_distance": {
                "_placeholder": True,
                "user_id": transaction["user_id"],
                "latitude": transaction["latitude"],
                "longitude": transaction["longitude"],
                "distance_km": None,
                "typical_location_label": None,
            }
        }
    }


def assemble_rationale(state: InvestigationState) -> dict:
    """Terminal node every branch converges on. No-op placeholder --
    SCRUM-53 fills this in with real evidence-grounded rationale
    composition, superseding the interim formatter as the primary source
    for the `rationale` field.
    """
    return {"rationale": ""}


def build_graph() -> StateGraph:
    graph = StateGraph(InvestigationState)

    graph.add_node("route_entry", route_entry)
    graph.add_node("get_transaction_history", get_transaction_history)
    graph.add_node("get_merchant_risk_score", get_merchant_risk_score)
    graph.add_node("get_geo_distance", get_geo_distance)
    graph.add_node("assemble_rationale", assemble_rationale)

    graph.set_entry_point("route_entry")
    graph.add_conditional_edges(
        "route_entry",
        route_to_tools,
        [*TOOL_NODE_NAMES, "assemble_rationale"],
    )
    for tool_node_name in TOOL_NODE_NAMES:
        graph.add_edge(tool_node_name, "assemble_rationale")
    graph.add_edge("assemble_rationale", END)

    return graph


# Compiled once at import time and exposed for direct, request-path-free use
# (e.g. investigation_graph.invoke({...}) from a script or test) until
# SCRUM-53 wires this into the live endpoint.
investigation_graph = build_graph().compile()
