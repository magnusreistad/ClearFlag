"""Investigation Agent StateGraph (SCRUM-47).

Routes evidence-gathering tool calls based on which rule(s) flagged a
transaction. The router and fan-out edges are scaffolding from SCRUM-47;
get_transaction_history / get_merchant_risk_score / get_geo_distance
(SCRUM-48/49/50) are the real evidence-gathering implementations dropped in
without touching that node/edge structure.

Not wired into the live FastAPI request path yet (that's SCRUM-53). The
interim formatter in app.rules.engine (concatenate_rationales) stays the
`rationale` field's source until then, and remains in the codebase
afterward as the on-failure fallback path (SCRUM-56).
"""

from collections import Counter
from datetime import timedelta
from typing import Literal

from langgraph.graph import END, StateGraph

from app.database import SessionLocal
from app.investigation_agent.state import InvestigationState
from app.models import Transaction
from app.rules.geographic_anomaly import MIN_HISTORY_COUNT, _centroid, _haversine_miles
from app.rules.velocity import DEFAULT_WINDOW_MINUTES

# 1 mile in kilometers, exact by definition (1 in = 2.54 cm) -- used to
# convert get_geo_distance's haversine-in-miles (the unit
# app.rules.geographic_anomaly itself thresholds on) into the km the
# SCRUM-50 contract asks for, without a second distance computation.
KM_PER_MILE = 1.609344

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
    """SCRUM-49. Triggered by the new_merchant_risk rule.

    Keys merchant identity off the `merchant` name string, not a merchant_id:
    there is no merchant_id column on Transaction and no merchants reference
    table anywhere in this codebase (confirmed by investigation), and
    app.rules.new_merchant_risk itself already keys "first transaction with
    a merchant" off the same name-string equality (`t.merchant not in
    seen_merchants`). Matching the rule's own identity notion is more
    correct than inventing a stricter one it doesn't use. This does mean
    two distinct real-world merchants sharing a display string would be
    treated as one -- a pre-existing fragility of the rule this tool is
    just reflecting, not a new one.

    prior_transaction_count/is_first_transaction are relative to the
    flagged transaction's own timestamp (strictly before it, not inclusive
    -- unlike get_transaction_history's window, this isn't counting the
    flagged transaction itself, just what came before it).

    risk_tier is always None: there is no merchant reference/risk-classification
    data source in this codebase to back it. The key stays present (rather
    than the field being omitted) to match the evidence shape SCRUM-47
    documented, with this comment standing in for that missing data source
    rather than a fabricated scoring scheme.

    Opens its own session via SessionLocal, same as get_transaction_history
    (SCRUM-48) and for the same reason: not on the FastAPI request path yet.
    """
    transaction = state["transaction"]
    user_id = transaction["user_id"]
    merchant = transaction["merchant"]
    anchor_timestamp = transaction["timestamp"]

    db = SessionLocal()
    try:
        prior_transaction_count = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == user_id,
                Transaction.merchant == merchant,
                Transaction.timestamp < anchor_timestamp,
            )
            .count()
        )
    finally:
        db.close()

    return {
        "evidence": {
            "get_merchant_risk_score": {
                "user_id": user_id,
                "merchant": merchant,
                "is_first_transaction": prior_transaction_count == 0,
                "prior_transaction_count": prior_transaction_count,
                "risk_tier": None,
            }
        }
    }


def _most_common_location_label(history: list[Transaction]) -> str:
    """Most frequent location_label among `history`. Ties (more than one
    label sharing the max count) are broken alphabetically, via min() over
    the tied labels -- arbitrary but deterministic, which a tiebreak only
    needs to be here since there's no notion of one label being more
    "correct" than another at equal frequency.
    """
    counts = Counter(t.location_label for t in history)
    max_count = max(counts.values())
    return min(label for label, count in counts.items() if count == max_count)


def get_geo_distance(state: InvestigationState) -> dict:
    """SCRUM-50. Triggered by the geographic_anomaly rule.

    Mirrors app.rules.geographic_anomaly's own math instead of reinventing
    it: the same prior-transaction set (this user's transactions strictly
    before the flagged one -- approximating the rule's ordered[:i] slice of
    its full-history sort the same way get_merchant_risk_score (SCRUM-49)
    approximates "prior" with strict timestamp <, rather than replaying the
    rule's exact index-in-full-sorted-history walk here), the same
    plain-mean lat/lon centroid over that set (_centroid), and the same
    haversine distance from that centroid to the flagged transaction
    (_haversine_miles) -- both imported from the rule module rather than
    reimplemented, so this tool can't silently drift from what the rule
    itself computed.

    The rule thresholds in miles (EARTH_RADIUS_MILES), but the SCRUM-50
    contract's output field is distance_km -- so both are returned:
    distance_miles is the exact figure the rule itself compared against its
    z-score threshold (for a rationale/guardrail to cite), distance_km is
    that same great-circle distance converted to km (KM_PER_MILE), not a
    second computation.

    typical_location_label comes from the same prior-transaction set's
    location_label column (the only location-descriptive column on
    Transaction), taking the most frequent value -- see
    _most_common_location_label for tie handling. No geocoding API is
    called; this is a lookup over data already in the transactions table.

    Never fabricates (Design Doc SS5): with fewer than
    geographic_anomaly.MIN_HISTORY_COUNT prior transactions -- the same
    floor the rule itself requires before it will compute a centroid at
    all -- distance_km, distance_miles, and typical_location_label are all
    None rather than a guess from too small a sample.

    Opens its own session via SessionLocal, same as get_transaction_history
    (SCRUM-48) and get_merchant_risk_score (SCRUM-49) and for the same
    reason: not on the FastAPI request path yet.
    """
    transaction = state["transaction"]
    user_id = transaction["user_id"]
    latitude = transaction["latitude"]
    longitude = transaction["longitude"]
    anchor_timestamp = transaction["timestamp"]

    db = SessionLocal()
    try:
        history = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == user_id,
                Transaction.timestamp < anchor_timestamp,
            )
            .order_by(Transaction.timestamp)
            .all()
        )
    finally:
        db.close()

    distance_km = None
    distance_miles = None
    typical_location_label = None

    if len(history) >= MIN_HISTORY_COUNT:
        centroid_lat, centroid_lon = _centroid(history)
        distance_miles = _haversine_miles(centroid_lat, centroid_lon, latitude, longitude)
        distance_km = distance_miles * KM_PER_MILE
        typical_location_label = _most_common_location_label(history)

    return {
        "evidence": {
            "get_geo_distance": {
                "user_id": user_id,
                "latitude": latitude,
                "longitude": longitude,
                "distance_km": distance_km,
                "distance_miles": distance_miles,
                "typical_location_label": typical_location_label,
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
