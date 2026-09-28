import operator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


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
    call ToolNode caught an exception from (see graph.py's assemble_rationale).
    Not Annotated with a reducer: unlike evidence, it's only ever written
    once, by assemble_rationale, after all tool execution has finished.
    """

    transaction: TransactionData
    rule_names: list[str]
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: Annotated[dict[str, dict], operator.or_]
    tool_errors: dict[str, str]
    rationale: str
