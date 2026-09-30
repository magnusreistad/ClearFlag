import operator
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.investigation_agent.validation import Violation

if TYPE_CHECKING:
    from app.models import Transaction


class TransactionData(TypedDict):
    """The subset of Transaction fields the Investigation Agent's tools need
    as input. Mirrors app.models.Transaction, not TransactionOut -- rule_names
    and rationale are this graph's output, not its input.
    """

    id: int
    user_id: int
    timestamp: datetime
    merchant: str
    category: str
    amount: Decimal
    latitude: float
    longitude: float
    location_label: str


def transaction_data_from_orm(transaction: "Transaction") -> TransactionData:
    """SCRUM-53 Phase B. Builds a TransactionData dict from a persisted
    app.models.Transaction row -- for callers that start from a real DB row
    (app.routers.transactions._refresh_flags's read-only agent_rationales
    lookup, scripts.compose_rationales) rather than a hand-built dict, e.g.
    a test's own TransactionData literal.
    """
    return {
        "id": transaction.id,
        "user_id": transaction.user_id,
        "timestamp": transaction.timestamp,
        "merchant": transaction.merchant,
        "category": transaction.category,
        "amount": transaction.amount,
        "latitude": transaction.latitude,
        "longitude": transaction.longitude,
        "location_label": transaction.location_label,
    }


class InvestigationState(TypedDict):
    """Shared state threaded through the Investigation Agent's StateGraph
    (SCRUM-47). One state per flagged-transaction investigation.

    evidence is keyed by tool name (e.g. "get_geo_distance") so parallel
    branches writing concurrently in the same superstep merge without
    clobbering each other -- each tool only ever writes its own key, so a
    plain dict union reducer is enough (SCRUM-47: confirmed parallel
    fan-out over sequential; SCRUM-51: the parallel writers are now
    ToolNode's tool executions rather than separate graph nodes, but the
    same merge requirement holds).

    messages (SCRUM-51) carries the single AIMessage plan_tool_calls emits
    (one tool_call per fired rule, built deterministically from
    RULE_TOOL_NODES -- never a model's choice, see graph.py) and the
    ToolMessages ToolNode appends when it executes them. add_messages is
    the standard reducer for this: it appends by message id rather than
    overwriting, which is what lets ToolNode's parallel tool executions
    each land their own ToolMessage without clobbering the others.

    tool_errors (SCRUM-51) records tool-name -> error-message for any tool
    call ToolNode caught an exception from (see graph.py's collect_evidence,
    renamed from assemble_rationale in SCRUM-53). Not Annotated with a
    reducer: unlike evidence, it's only ever written once, by
    collect_evidence, after all tool execution has finished.

    rule_values (SCRUM-53) is keyed by rule name -- e.g. the per-transaction
    entry from app.rules.engine.rule_values_by_transaction() -- and is
    compose_rationale's other input alongside transaction/rule_names/evidence
    for building the build_payload() facts the model is shown. Supplied by
    the caller at invocation time, same as transaction and rule_names; this
    graph never computes it itself.

    rationale_source, violations, and composition_error are all written once,
    by compose_rationale/validate (SCRUM-53): rationale_source is "agent" on
    a validated composed rationale or "interim" whenever composition/
    validation didn't produce a trustworthy one (SCRUM-56 decides what the
    caller does with "interim" -- this graph never falls back to the interim
    formatter's text itself, it just signals that a caller should).
    violations carries validate_rationale's failures for that logging.
    composition_error carries a readable record of why composition didn't
    produce a trustworthy rationale (SCRUM-56): "ToolError[<tool>]:
    <ExceptionClass>: <message>" (one or more, joined by "; ") when
    tool_errors was non-empty and no model call was even attempted, or
    "ModelError: <ExceptionClass>(<status_code or ->): <message>" when the
    model call itself raised after get_chat_model()'s own SDK retries were
    exhausted. composition_error_label is the same information reduced to
    exception class names only (no message) -- log-safe, since a tool or
    model exception's message can embed values (merchant, lat/long, API
    response text) that shouldn't leave agent_rationales into application
    logs; see scripts.compose_rationales's per-attempt log line, the only
    reader of this key.

    composed_rationale (SCRUM-53 follow-up) is written once by
    compose_rationale and NEVER touched by validate -- unlike `rationale`,
    which validate overwrites to "" on a failed validation, this key always
    holds exactly what the model produced this run (or "" if the model call
    itself raised). It exists so a failed attempt's actual text survives to
    the final state for scripts.compose_rationales to persist into
    app.models.AgentRationale.composed_text (the audit record), even though
    `rationale`/rationale_source correctly still report the composition as
    untrustworthy.
    """

    transaction: TransactionData
    rule_names: list[str]
    rule_values: dict[str, dict[str, Any]]
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: Annotated[dict[str, dict], operator.or_]
    tool_errors: dict[str, str]
    rationale: str
    composed_rationale: str
    rationale_source: str
    violations: list[Violation]
    composition_error: str | None
    composition_error_label: str | None
