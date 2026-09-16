import operator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, TypedDict


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
    fan-out over sequential, see graph.py's route_to_tools).
    """

    transaction: TransactionData
    rule_names: list[str]
    evidence: Annotated[dict[str, dict], operator.or_]
    rationale: str
