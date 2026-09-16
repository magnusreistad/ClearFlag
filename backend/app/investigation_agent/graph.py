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

from datetime import timedelta
from typing import Literal

from langgraph.graph import END, StateGraph

from app.database import SessionLocal
from app.investigation_agent.state import InvestigationState
from app.models import Transaction
from app.rules.velocity import DEFAULT_WINDOW_MINUTES

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
    """SCRUM-48. Triggered by the velocity rule.

    Queries the same rolling window the velocity rule itself flags on --
    DEFAULT_WINDOW_MINUTES ending at the flagged transaction's own timestamp
    (see app.rules.velocity) -- so the evidence lines up with the count the
    rule already computed. The window is inclusive of both endpoints,
    matching velocity.py's `> window` (not `>=`) exclusion test, and
    includes the flagged transaction itself since the rule's own count does.

    Opens its own session via SessionLocal (module-level, so tests can
    monkeypatch it) rather than taking a `db` parameter: this node isn't on
    the FastAPI request path yet (that's SCRUM-53), so there's no
    request-scoped session to inject.
    """
    transaction = state["transaction"]
    user_id = transaction["user_id"]
    window_end = transaction["timestamp"]
    window_start = window_end - timedelta(minutes=DEFAULT_WINDOW_MINUTES)

    db = SessionLocal()
    try:
        rows = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == user_id,
                Transaction.timestamp >= window_start,
                Transaction.timestamp <= window_end,
            )
            .order_by(Transaction.timestamp)
            .all()
        )
    finally:
        db.close()

    transactions = [{"timestamp": row.timestamp, "amount": row.amount, "merchant": row.merchant} for row in rows]

    return {
        "evidence": {
            "get_transaction_history": {
                "user_id": user_id,
                "transactions": transactions,
                "count": len(transactions),
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
