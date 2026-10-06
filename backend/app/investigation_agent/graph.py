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

SCRUM-53 adds the rest of the pipeline -- collect_evidence (renamed from
assemble_rationale, mapping unchanged) -> compose_rationale (asks the model,
via app.investigation_agent.llm.get_chat_model, to compose one rationale
from build_payload()+evidence+derived facts) -> validate (SCRUM-52's
validate_rationale as the final citation gate) -- but still doesn't wire
this graph into the live FastAPI request path itself; that's a separate,
not-yet-approved integration decision. The interim formatter in
app.rules.engine (concatenate_rationales) remains in the codebase as the
fallback whenever compose_rationale/validate don't produce a trustworthy
rationale (rationale_source="interim" -- SCRUM-56 owns what a caller does
with that signal).
"""

import logging
import os
import re
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from sqlalchemy import text

from app.database import SessionLocal
from app.investigation_agent.derived_facts import compute_derived_facts
from app.investigation_agent.llm import get_chat_model
from app.investigation_agent.payload import build_payload
from app.investigation_agent.prompts import build_prompt
from app.investigation_agent.state import InvestigationState, TransactionData
from app.investigation_agent.tracing import (
    enforce_no_tracing_in_ci,
    invoke_investigation_graph,
)
from app.investigation_agent.validation import validate_rationale
from app.models import Transaction
from app.rules.geographic_anomaly import MIN_HISTORY_COUNT, _centroid, _haversine_miles
from app.rules.velocity import DEFAULT_WINDOW_MINUTES

logger = logging.getLogger(__name__)

# 1 mile in kilometers, exact by definition (1 in = 2.54 cm) -- used to
# convert get_geo_distance's haversine-in-miles (the unit
# app.rules.geographic_anomaly itself thresholds on) into the km the
# SCRUM-50 contract asks for, without a second distance computation.
KM_PER_MILE = 1.609344

# SCRUM-56: a tool exception's or model exception's message is truncated to
# this many characters before it goes into composition_error -- long enough
# to stay useful for an audit, short enough that a verbose DB/API error
# message (which can run to several KB) doesn't bloat the column.
_MAX_ERROR_MESSAGE_CHARS = 300


def _truncate(text_value: str, limit: int = _MAX_ERROR_MESSAGE_CHARS) -> str:
    return text_value if len(text_value) <= limit else text_value[:limit] + "…"


# SCRUM-56: Postgres statement_timeout (milliseconds) applied only to this
# module's own tool sessions (_tool_session below), not the FastAPI
# request-path engine -- so a slow/hung DB call inside a tool becomes a
# catchable tool error (ToolNode's handle_tool_errors=True) instead of
# hanging composition indefinitely. Env-overridable, same convention as
# app.investigation_agent.llm's DEFAULT_TIMEOUT_SECONDS.
DEFAULT_TOOL_DB_TIMEOUT_MS = 5000


@contextmanager
def _tool_session():
    """Opens a SessionLocal() session for a tool's DB query, applying
    DEFAULT_TOOL_DB_TIMEOUT_MS (or INVESTIGATION_AGENT_TOOL_DB_TIMEOUT_MS)
    as a Postgres `SET LOCAL statement_timeout` on that session alone.
    Skipped for any non-Postgres dialect, which has no statement_timeout
    equivalent (every environment, tests included, runs Postgres since
    SCRUM-67). timeout_ms is validated by int() (a
    bad env var is a config error, surfaced immediately) before going into
    the SQL text, so this is never user-controlled string interpolation.
    """
    db = SessionLocal()
    try:
        if db.bind.dialect.name == "postgresql":
            timeout_ms = int(os.getenv("INVESTIGATION_AGENT_TOOL_DB_TIMEOUT_MS", DEFAULT_TOOL_DB_TIMEOUT_MS))
            db.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
        yield db
    finally:
        db.close()


# SCRUM-56: parses a ToolNode(handle_tool_errors=True) error ToolMessage's
# content back into (exception class, message). That content is always
# TOOL_CALL_ERROR_TEMPLATE.format(error=repr(exc)) --
# "Error: <ClassName>(<repr'd args>)\n Please fix your mistakes." -- langgraph
# doesn't expose the original exception object past ToolNode, only this
# formatted string.
_TOOL_ERROR_CONTENT_RE = re.compile(
    r"^Error:\s*(?P<exc_class>[\w.]+)\((?P<message>.*)\)\s*\n Please fix your mistakes\.$",
    re.DOTALL,
)


def _parse_tool_error(content: str) -> tuple[str, str]:
    """Best-effort split of an error ToolMessage's content into (exception
    class name, message) for a readable composition_error. Falls back to
    ("ToolError", content) if the content doesn't match langgraph's current
    template (e.g. a future langgraph version changes it) -- a parsing
    surprise degrades to "still logged, less neatly", never a crash.
    """
    match = _TOOL_ERROR_CONTENT_RE.match(content)
    if not match:
        return "ToolError", content
    exc_class = match.group("exc_class")
    message = match.group("message")
    # repr()'s single string arg is usually quoted, e.g. 'boom' -- strip
    # matching outer quotes for readability; anything else (multi-arg
    # exceptions, no quotes) is left untouched.
    if len(message) >= 2 and message[0] == message[-1] and message[0] in ("'", '"'):
        message = message[1:-1]
    return exc_class, message

# SCRUM-51: which tool each rule's evidence comes from -- the single source
# of truth both plan_tool_calls (which tool_calls to build) and
# collect_evidence (comment below) rely on. amount_deviation has no entry:
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

    Opens its own session via _tool_session() (SCRUM-56: SessionLocal,
    module-level so tests can monkeypatch it, plus the configurable Postgres
    statement_timeout) rather than taking a `db` parameter: this tool isn't
    on the FastAPI request path yet, so there's no request-scoped session to
    inject.

    response_format="content_and_artifact" (SCRUM-51): `content` is a short
    string for the trace, `artifact` is the same structured evidence dict
    this returned pre-SCRUM-51 -- collect_evidence reads the artifact back
    into state["evidence"]["get_transaction_history"] unchanged, so nothing
    downstream re-parses a JSON string.
    """
    window_start = anchor_timestamp - timedelta(minutes=window_minutes)

    with _tool_session() as db:
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

    Opens its own session via _tool_session(), same as get_transaction_history
    (SCRUM-48) and for the same reason: not on the FastAPI request path yet.
    """
    with _tool_session() as db:
        prior_transaction_count = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == user_id,
                Transaction.merchant == merchant,
                Transaction.timestamp < anchor_timestamp,
            )
            .count()
        )

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

    Opens its own session via _tool_session(), same as get_transaction_history
    (SCRUM-48) and get_merchant_risk_score (SCRUM-49) and for the same
    reason: not on the FastAPI request path yet.
    """
    with _tool_session() as db:
        history = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == user_id,
                Transaction.timestamp < anchor_timestamp,
            )
            .order_by(Transaction.timestamp)
            .all()
        )

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


def collect_evidence(state: InvestigationState) -> dict:
    """SCRUM-53: renamed from assemble_rationale, mapping unchanged -- this
    node's job is now purely gathering evidence out of the message log, not
    also standing in as a rationale placeholder. compose_rationale (below)
    is what actually produces `rationale` now, and this node no longer
    writes that key at all.

    SCRUM-51: maps ToolNode's output -- ToolMessages appended to
    state["messages"] -- into the per-tool `evidence` keys the rest of the
    graph (and the Investigation Agent Design Doc's payload/evidence
    contract) expects. A ToolMessage with status == "error" (see
    build_graph's handle_tool_errors=True) means that tool call raised; its
    failure is recorded in tool_errors keyed by tool name and its evidence
    key is left absent entirely -- never populated with a substitute value
    (Design Doc: never fabricate). Parallel tool calls fail independently:
    one tool erroring doesn't prevent another's evidence from landing here.

    Every branch converges here, including the no-tool amount_deviation-only
    path (plan_tool_calls's empty tool_calls list routes tools_condition
    straight past ToolNode) -- state["messages"] then has no ToolMessages at
    all, so both dicts come back empty, which is correct: there's nothing to
    map.
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

    return {"evidence": evidence, "tool_errors": tool_errors}


def compose_rationale(state: InvestigationState) -> dict:
    """SCRUM-53. Builds this invocation's citable facts -- build_payload()
    (the flagged transaction's own snapshot plus each fired rule's own
    RuleHit.values, SCRUM-68) combined with evidence (collect_evidence's
    output) and compute_derived_facts() (SCRUM-52's pre-computed derivations,
    e.g. amount_deviation's percent_above_mean) -- and asks the model, via
    get_chat_model() (SCRUM-55; the ONLY place this graph ever constructs a
    chat model), to compose one coherent rationale addressing every fired
    rule. The model is shown exactly these facts as structured JSON (see
    app.investigation_agent.prompts.build_prompt) and nothing else -- no
    database access, no other context.

    Never retries and never crashes the graph on a model-call failure
    (timeout, API error, ...): the SDK's own retries already ran inside
    get_chat_model()'s client (INVESTIGATION_AGENT_LLM_MAX_RETRIES); once
    those are exhausted the exception is caught here and recorded in
    composition_error, with `rationale` left empty. validate() (below) never
    runs its grounding checks on a composition_error -- an empty rationale
    from a genuine failure isn't a citation problem, and treating it as one
    would misrepresent what happened in `violations`.

    SCRUM-56: tool failures short-circuit here too, before any model call --
    state["tool_errors"] (collect_evidence) is checked first, and a non-empty
    one means composition never proceeds for this transaction at all (no
    get_chat_model()/model.invoke() call, and no error text passed to the
    model as evidence). composition_error is built from ALL failed tools
    (not just the first), each rendered "ToolError[<tool>]: <ExceptionClass>:
    <message>" via _parse_tool_error -- collect_evidence already keeps a
    failing tool's evidence key absent, so this is really just turning that
    into an audit-friendly string; a model failure (below) is instead
    rendered "ModelError: <ExceptionClass>(<status_code or ->):
    <message>", using getattr(exc, "status_code", None) since not every
    exception type carries one (e.g. a plain TimeoutError). Both messages are
    truncated (_MAX_ERROR_MESSAGE_CHARS) before going into the column, and
    the model failure is also logged via logger.exception so the traceback
    isn't lost to "caught and stringified".

    enforce_no_tracing_in_ci() runs here, not only in the optional
    invoke_investigation_graph wrapper (app.investigation_agent.tracing):
    this is the one place in the graph that can start a trace at all (the
    model call below), and tests/callers that invoke investigation_graph
    directly -- bypassing that wrapper -- must still never make a tracing
    network call in CI.
    """
    enforce_no_tracing_in_ci()

    transaction = state["transaction"]

    tool_errors = state.get("tool_errors", {})
    if tool_errors:
        parts = []
        labels = []
        for tool_name in sorted(tool_errors):
            exc_class, message = _parse_tool_error(str(tool_errors[tool_name]))
            parts.append(f"ToolError[{tool_name}]: {exc_class}: {_truncate(message)}")
            labels.append(f"ToolError[{tool_name}]:{exc_class}")
        return {
            "rationale": "",
            "composed_rationale": "",
            "composition_error": "; ".join(parts),
            # SCRUM-56: a log-safe summary of composition_error -- class
            # names only, never the message (which can embed a tool's own
            # query args, e.g. merchant/lat-long) -- for
            # scripts.compose_rationales's per-attempt log line. The full
            # composition_error string above is what gets persisted.
            "composition_error_label": "; ".join(labels),
        }

    payload = build_payload(transaction, state["rule_names"], state.get("rule_values", {}))
    evidence = state.get("evidence", {})
    derived = compute_derived_facts(payload)

    prompt = build_prompt(payload, evidence, derived)

    try:
        model = get_chat_model()
        response = model.invoke(prompt)
        # SCRUM-53 Phase A live check: a live model can return `content` as a
        # list of content blocks rather than a plain string -- e.g. an
        # extended-thinking block alongside the actual text block -- and
        # `str(response.content)` on that list stringifies the WHOLE list,
        # thinking block (and its large base64 signature) included, as the
        # "rationale". `.text` is AIMessage's own accessor for exactly this:
        # it extracts and concatenates only the text-type blocks, and still
        # returns a plain string unchanged when content already was one.
        content = response.text
    except Exception as exc:
        logger.exception("Investigation Agent model call failed for transaction %s", transaction["id"])
        status_code = getattr(exc, "status_code", None)
        status_display = status_code if status_code is not None else "-"
        composition_error = f"ModelError: {type(exc).__name__}({status_display}): {_truncate(str(exc))}"
        return {
            "rationale": "",
            "composed_rationale": "",
            "composition_error": composition_error,
            "composition_error_label": f"ModelError:{type(exc).__name__}({status_display})",
        }

    stripped = content.strip()
    # composed_rationale (SCRUM-53 follow-up) is the audit record of what
    # was actually produced this run -- written here and never touched by
    # validate() below, unlike `rationale`, which validate overwrites to ""
    # on a failed validation. See InvestigationState's docstring.
    return {
        "rationale": stripped,
        "composed_rationale": stripped,
        "composition_error": None,
        "composition_error_label": None,
    }


def validate(state: InvestigationState) -> dict:
    """SCRUM-53. The final gate before a composed rationale is trusted:
    SCRUM-52's validate_rationale() re-derives the exact same citable facts
    compose_rationale showed the model (same build_payload + evidence
    inputs) and checks the composed `rationale` against them.

    On pass, state carries the composed rationale unchanged with
    rationale_source="agent". On failure -- rationale_source="interim" --
    the composed rationale is discarded entirely (never repaired or
    partially accepted, per the Design Doc), so a caller falls back to the
    interim formatter's output (SCRUM-56). violations is kept in state
    either way (empty on a pass) for that caller's failure logging.
    Deliberately never returns/touches composed_rationale: that key is
    compose_rationale's own audit record of what was actually produced, and
    must survive a failed validation unchanged (see InvestigationState's
    docstring) -- this function only ever discards/keeps `rationale`, the
    "safe to serve" value.

    SCRUM-56: a composition_error (a tool failure or a model failure that
    compose_rationale already caught) skips validate_rationale entirely and
    goes straight to rationale_source="interim" with violations=[] -- an
    empty `rationale` in that case is a structural consequence of the
    failure, not a citation problem, and running the grounding checks on it
    would otherwise record a generic empty_rationale violation that
    misrepresents what actually happened (a tool/model failure, not a
    validation failure).
    """
    if state.get("composition_error"):
        return {"rationale": "", "rationale_source": "interim", "violations": []}

    transaction = state["transaction"]
    payload = build_payload(transaction, state["rule_names"], state.get("rule_values", {}))
    evidence = state.get("evidence", {})
    rationale = state.get("rationale", "")

    result = validate_rationale(rationale, payload, evidence)

    if result.passed:
        return {"rationale": rationale, "rationale_source": "agent", "violations": []}
    return {"rationale": "", "rationale_source": "interim", "violations": result.violations}


def build_graph() -> StateGraph:
    """SCRUM-53 target shape: plan_tool_calls -> tools_condition ->
    ToolNode -> collect_evidence -> compose_rationale -> validate -> END.
    The no-tool path (tools_condition's "__end__" branch, e.g. an
    amount_deviation-only flag) also lands on collect_evidence rather than
    skipping straight to composition -- there is exactly one path into
    compose_rationale, tool-branch or not.
    """
    graph = StateGraph(InvestigationState)

    graph.add_node("route_entry", route_entry)
    graph.add_node("plan_tool_calls", plan_tool_calls)
    # handle_tool_errors=True (SCRUM-51 groundwork for SCRUM-56): turns any
    # exception raised inside a tool into an error ToolMessage instead of
    # crashing the graph, so collect_evidence can record it in tool_errors.
    # The langgraph default only catches invalid-arguments errors and
    # re-raises everything else -- too narrow for "a DB call inside a tool
    # blew up shouldn't take down the whole investigation".
    graph.add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
    graph.add_node("collect_evidence", collect_evidence)
    graph.add_node("compose_rationale", compose_rationale)
    graph.add_node("validate", validate)

    graph.set_entry_point("route_entry")
    graph.add_edge("route_entry", "plan_tool_calls")
    graph.add_conditional_edges(
        "plan_tool_calls",
        tools_condition,
        {"tools": "tools", "__end__": "collect_evidence"},
    )
    graph.add_edge("tools", "collect_evidence")
    graph.add_edge("collect_evidence", "compose_rationale")
    graph.add_edge("compose_rationale", "validate")
    graph.add_edge("validate", END)

    return graph


# Compiled once at import time and exposed for direct, request-path-free use
# (e.g. investigation_graph.invoke({...}) from a script or test) until a
# future ticket wires this into the live endpoint (see this module's
# docstring). Prefer invoke() below when LangSmith trace metadata is wanted
# (SCRUM-53); tests exercising graph mechanics directly still use
# investigation_graph.invoke(...) itself.
investigation_graph = build_graph().compile()


def invoke(state: InvestigationState) -> InvestigationState:
    """SCRUM-53. investigation_graph.invoke() wrapped with LangSmith run
    metadata via app.investigation_agent.tracing.invoke_investigation_graph
    -- transaction_id, fired rule names, PROMPT_VERSION, model id up front,
    and rationale_source patched on afterward if tracing is actually
    enabled. Purely additive: behaves exactly like
    investigation_graph.invoke(state) when tracing is off (the default) or
    in CI (always forced off regardless).
    """
    return invoke_investigation_graph(investigation_graph, state)
