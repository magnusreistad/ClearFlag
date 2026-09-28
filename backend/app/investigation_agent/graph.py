"""Investigation Agent StateGraph (SCRUM-47).

Routes evidence-gathering tool calls based on which rule(s) flagged a
transaction. get_transaction_history / get_merchant_risk_score /
get_geo_distance (SCRUM-48/49/50) are the real evidence-gathering
implementations.

SCRUM-51 wires those three onto LangGraph's ToolNode/tools_condition,
replacing SCRUM-47's per-tool nodes and manual fan-out edge (route_to_tools)
with the prebuilt tool-calling primitives. Deliberately NOT .bind_tools() +
an LLM, despite the ticket text: the Investigation Agent Design Doc requires
tool selection to be a deterministic function of which rules fired, for
auditability -- no model gets discretion over what evidence gets gathered.
plan_tool_calls builds that AIMessage in code from RULE_TOOL_NODES; ToolNode
just executes whatever tool_calls it's handed. No LLM, no API key, no
network call anywhere in this module.

Not wired into the live FastAPI request path yet (that's SCRUM-53). The
interim formatter in app.rules.engine (concatenate_rationales) stays the
`rationale` field's source until then, and remains in the codebase
afterward as the on-failure fallback path (SCRUM-56).
"""

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.database import SessionLocal
from app.investigation_agent.state import InvestigationState, TransactionData
from app.models import Transaction
from app.rules.geographic_anomaly import MIN_HISTORY_COUNT, _centroid, _haversine_miles
from app.rules.velocity import DEFAULT_WINDOW_MINUTES

# 1 mile in kilometers, exact by definition (1 in = 2.54 cm) -- used to
# convert get_geo_distance's haversine-in-miles (the unit
# app.rules.geographic_anomaly itself thresholds on) into the km the
# SCRUM-50 contract asks for, without a second distance computation.
KM_PER_MILE = 1.609344

# SCRUM-51: which tool each rule's evidence comes from -- the single source
# of truth both plan_tool_calls (which tool_calls to build) and
# assemble_rationale (comment below) rely on. amount_deviation has no entry:
# its values (mean, stdev, actual amount) are already computed by the rules
# engine and passed in the invocation payload, so it needs no tool call.
RULE_TOOL_NODES: dict[str, str] = {
    "velocity": "get_transaction_history",
    "new_merchant_risk": "get_merchant_risk_score",
    "geographic_anomaly": "get_geo_distance",
}


def route_entry(state: InvestigationState) -> dict:
    """Entry node. No-op passthrough -- exists as the fixed anchor
    plan_tool_calls hangs off of."""
    return {}


@tool(response_format="content_and_artifact")
def get_transaction_history(
    user_id: int, anchor_timestamp: datetime, window_minutes: int = DEFAULT_WINDOW_MINUTES
) -> tuple[str, dict]:
    """SCRUM-48. Triggered by the velocity rule.

    Queries the same rolling window the velocity rule itself flags on --
    window_minutes (plan_tool_calls passes DEFAULT_WINDOW_MINUTES) ending at
    the flagged transaction's own timestamp (see app.rules.velocity) -- so
    the evidence lines up with the count the rule already computed. The
    window is inclusive of both endpoints, matching velocity.py's `> window`
    (not `>=`) exclusion test, and includes the flagged transaction itself
    since the rule's own count does.

    Opens its own session via SessionLocal (module-level, so tests can
    monkeypatch it) rather than taking a `db` parameter: this tool isn't on
    the FastAPI request path yet (that's SCRUM-53), so there's no
    request-scoped session to inject.

    response_format="content_and_artifact" (SCRUM-51): `content` is a short
    string for the trace, `artifact` is the same structured evidence dict
    this returned pre-SCRUM-51 -- assemble_rationale reads the artifact back
    into state["evidence"]["get_transaction_history"] unchanged, so nothing
    downstream re-parses a JSON string.
    """
    window_start = anchor_timestamp - timedelta(minutes=window_minutes)

    db = SessionLocal()
    try:
        rows = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == user_id,
                Transaction.timestamp >= window_start,
                Transaction.timestamp <= anchor_timestamp,
            )
            .order_by(Transaction.timestamp)
            .all()
        )
    finally:
        db.close()

    transactions = [{"timestamp": row.timestamp, "amount": row.amount, "merchant": row.merchant} for row in rows]

    evidence = {
        "user_id": user_id,
        "transactions": transactions,
        "count": len(transactions),
    }
    return f"Found {len(transactions)} transaction(s) in the {window_minutes}-minute window.", evidence


@tool(response_format="content_and_artifact")
def get_merchant_risk_score(user_id: int, merchant: str, anchor_timestamp: datetime) -> tuple[str, dict]:
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

    prior_transaction_count/is_first_transaction are relative to
    anchor_timestamp (strictly before it, not inclusive -- unlike
    get_transaction_history's window, this isn't counting the flagged
    transaction itself, just what came before it).

    risk_tier is always None: there is no merchant reference/risk-classification
    data source in this codebase to back it. The key stays present (rather
    than the field being omitted) to match the evidence shape SCRUM-47
    documented, with this comment standing in for that missing data source
    rather than a fabricated scoring scheme.

    Opens its own session via SessionLocal, same as get_transaction_history
    (SCRUM-48) and for the same reason: not on the FastAPI request path yet.
    """
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

    evidence = {
        "user_id": user_id,
        "merchant": merchant,
        "is_first_transaction": prior_transaction_count == 0,
        "prior_transaction_count": prior_transaction_count,
        "risk_tier": None,
    }
    label = "first" if prior_transaction_count == 0 else f"not first ({prior_transaction_count} prior)"
    return f"{merchant}: {label} transaction for this user.", evidence


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


@tool(response_format="content_and_artifact")
def get_geo_distance(user_id: int, latitude: float, longitude: float, anchor_timestamp: datetime) -> tuple[str, dict]:
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

    evidence = {
        "user_id": user_id,
        "latitude": latitude,
        "longitude": longitude,
        "distance_km": distance_km,
        "distance_miles": distance_miles,
        "typical_location_label": typical_location_label,
    }
    if distance_km is None:
        content = f"Insufficient history (fewer than {MIN_HISTORY_COUNT} prior transactions) to compute a distance."
    else:
        content = f"{distance_miles:.1f} miles from this user's typical location ({typical_location_label})."
    return content, evidence


TOOLS = (get_transaction_history, get_merchant_risk_score, get_geo_distance)

# SCRUM-51: builds each tool's args from the flagged transaction, keyed by
# tool name -- the counterpart to RULE_TOOL_NODES (which rule maps to which
# tool) that plan_tool_calls uses to build that tool's call.
_TOOL_CALL_ARGS: dict[str, Callable[[TransactionData], dict]] = {
    "get_transaction_history": lambda transaction: {
        "user_id": transaction["user_id"],
        "anchor_timestamp": transaction["timestamp"],
        "window_minutes": DEFAULT_WINDOW_MINUTES,
    },
    "get_merchant_risk_score": lambda transaction: {
        "user_id": transaction["user_id"],
        "merchant": transaction["merchant"],
        "anchor_timestamp": transaction["timestamp"],
    },
    "get_geo_distance": lambda transaction: {
        "user_id": transaction["user_id"],
        "latitude": transaction["latitude"],
        "longitude": transaction["longitude"],
        "anchor_timestamp": transaction["timestamp"],
    },
}


def plan_tool_calls(state: InvestigationState) -> dict:
    """SCRUM-51. Builds one tool_call per triggered rule that maps to a tool
    (RULE_TOOL_NODES), deterministically from state -- never from model
    discretion. This is the Design Doc's auditability requirement: this
    ticket intentionally does NOT use .bind_tools(), so nothing here is a
    model's choice of what evidence to gather.

    Emits a single AIMessage carrying every tool_call for this invocation
    (possibly zero, e.g. an amount_deviation-only flag) rather than one
    message per call: tools_condition inspects exactly the last message's
    tool_calls list, and ToolNode still executes a multi-call AIMessage's
    tool_calls in parallel, so SCRUM-47's parallel fan-out is preserved.

    Tool call ids are derived from tool name + transaction id (not a random
    uuid) so they're stable across repeated runs of the same transaction --
    useful for traces and for keeping test assertions deterministic.

    Rules are deduplicated via dict.fromkeys (not a set) so tool_calls stays
    in RULE_TOOL_NODES iteration order rather than arbitrary hash order,
    even though today's 1:1 rule->tool mapping means no rule can actually
    produce a duplicate.
    """
    transaction = state["transaction"]
    tool_names = dict.fromkeys(
        RULE_TOOL_NODES[rule] for rule in state["rule_names"] if rule in RULE_TOOL_NODES
    )
    tool_calls = [
        {
            "name": tool_name,
            "args": _TOOL_CALL_ARGS[tool_name](transaction),
            "id": f"{tool_name}-{transaction['id']}",
            "type": "tool_call",
        }
        for tool_name in tool_names
    ]
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


def assemble_rationale(state: InvestigationState) -> dict:
    """Terminal node every branch converges on.

    SCRUM-51: maps ToolNode's output -- ToolMessages appended to
    state["messages"] -- back into the same per-tool `evidence` keys the
    state used before ToolNode existed, so SCRUM-53's contract doesn't
    change. A ToolMessage with status == "error" (see build_graph's
    handle_tool_errors=True) means that tool call raised; its failure is
    recorded in tool_errors keyed by tool name and its evidence key is left
    absent entirely -- never populated with a substitute value (Design Doc:
    never fabricate). Parallel tool calls fail independently: one tool
    erroring doesn't prevent another's evidence from landing here.

    Rationale composition itself is still a no-op placeholder -- SCRUM-53
    fills that in, superseding the interim formatter as the primary source
    for the `rationale` field.
    """
    evidence: dict[str, dict] = {}
    tool_errors: dict[str, str] = {}
    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage):
            continue
        if message.status == "error":
            tool_errors[message.name] = str(message.content)
        else:
            evidence[message.name] = message.artifact

    return {"evidence": evidence, "tool_errors": tool_errors, "rationale": ""}


def build_graph() -> StateGraph:
    graph = StateGraph(InvestigationState)

    graph.add_node("route_entry", route_entry)
    graph.add_node("plan_tool_calls", plan_tool_calls)
    # handle_tool_errors=True (SCRUM-51 groundwork for SCRUM-56): turns any
    # exception raised inside a tool into an error ToolMessage instead of
    # crashing the graph, so assemble_rationale can record it in
    # tool_errors. The langgraph default only catches invalid-arguments
    # errors and re-raises everything else -- too narrow for "a DB call
    # inside a tool blew up shouldn't take down the whole investigation".
    graph.add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
    graph.add_node("assemble_rationale", assemble_rationale)

    graph.set_entry_point("route_entry")
    graph.add_edge("route_entry", "plan_tool_calls")
    graph.add_conditional_edges(
        "plan_tool_calls",
        tools_condition,
        {"tools": "tools", "__end__": "assemble_rationale"},
    )
    graph.add_edge("tools", "assemble_rationale")
    graph.add_edge("assemble_rationale", END)

    return graph


# Compiled once at import time and exposed for direct, request-path-free use
# (e.g. investigation_graph.invoke({...}) from a script or test) until
# SCRUM-53 wires this into the live endpoint.
investigation_graph = build_graph().compile()
