"""SCRUM-54: the one shared fixture module for Investigation Agent tests.

Tool fixtures are deterministic seeded rows, one scenario per rule type: the
evidence tools are never mocked (SCRUM-55), they run real queries against
the test Postgres. Each scenario is hand-built (no RNG -- unlike
scripts.seed_transactions) so that exactly the intended rule(s) fire, and
seed_scenario() checks that with evaluate_all_rules before returning, so a
rule change that breaks a scenario fails loudly at seed time rather than
showing up as a confusing downstream assertion.

LLM completions are the only thing scripted, and always through the real
app.investigation_agent.llm.get_chat_model(responses=...) -- see
ScriptedLLM. Tests assert on state, routing, persisted rows and validation
outcomes; the scripted texts below only need to be grounded or not, never
to say anything in particular.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import anthropic
import httpx
import pytest
from langchain_core.messages import AIMessage

from app.database import SessionLocal
from app.investigation_agent import graph as graph_module
from app.investigation_agent import llm as llm_module
from app.investigation_agent.prompts import MAX_RATIONALE_CHARS
from app.investigation_agent.state import InvestigationState, transaction_data_from_orm
from app.models import Transaction, User
from app.rules.engine import (
    evaluate_all_rules,
    rule_names_by_transaction,
    rule_values_by_transaction,
)

BASE_TIME = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
HOME = {"latitude": 47.6062, "longitude": -122.3321, "location_label": "Seattle, WA"}
MANILA = {"latitude": 14.5995, "longitude": 120.9842, "location_label": "Manila, Philippines"}


def _txn(user_id: int, timestamp: datetime, *, merchant: str = "Corner Grocer", category: str = "groceries",
         amount: str = "10.00", place: dict = HOME) -> Transaction:
    return Transaction(user_id=user_id, timestamp=timestamp, merchant=merchant, category=category,
                       amount=Decimal(amount), **place)


def _daily(user_id: int, count: int, **kwargs) -> list[Transaction]:
    """`count` identical transactions, one per day, ending the day before BASE_TIME."""
    return [_txn(user_id, BASE_TIME - timedelta(days=count - i), **kwargs) for i in range(count)]


def _first_purchases(user_id: int, count: int, *, category: str) -> list[Transaction]:
    """`count` first-time purchases at distinct merchants, $10 each -- the
    history new_merchant_risk needs (MIN_HISTORY_COUNT earlier first purchases)."""
    return [
        _txn(user_id, BASE_TIME - timedelta(days=count - i), merchant=f"Shop {chr(ord('A') + i)} Market",
             category=category)
        for i in range(count)
    ]


def _seed_velocity(user_id):
    # 6 transactions inside the 10-minute window (velocity: more than 5).
    # The rule flags every member of the burst; the last is the primary.
    burst = [_txn(user_id, BASE_TIME - timedelta(minutes=5 - i)) for i in range(6)]
    return burst, burst[-1]


def _seed_amount_deviation(user_id, *, category: str = "groceries"):
    outlier = _txn(user_id, BASE_TIME, category=category, amount="500.00")
    return [*_daily(user_id, 5, category=category), outlier], outlier


def add_amount_deviation_outlier(db, user_id: int, *, category: str = "groceries") -> int:
    """The amount_deviation scenario's rows for an existing user, in
    `category` -- for tests that need several independent flagged
    transactions for one user (one per category). Flushes; returns the
    outlier's id."""
    transactions, outlier = _seed_amount_deviation(user_id, category=category)
    db.add_all(transactions)
    db.flush()
    return outlier.id


def _seed_new_merchant_risk(user_id):
    # Different category from the first-purchase history, so amount_deviation
    # (which needs 5 prior transactions in the same category) can't fire.
    outlier = _txn(user_id, BASE_TIME, merchant="Bright Circuit Electronics", category="electronics", amount="500.00")
    return [*_first_purchases(user_id, 5, category="groceries"), outlier], outlier


def _seed_geographic_anomaly(user_id):
    outlier = _txn(user_id, BASE_TIME, place=MANILA)
    return [*_daily(user_id, 5), outlier], outlier


def _seed_meridian_triple(user_id):
    """SCRUM-65's shape: new merchant + amount outlier + far away, no velocity."""
    outlier = _txn(user_id, BASE_TIME, merchant="Meridian Duty-Free Traders", category="shopping",
                   amount="3200.00", place=MANILA)
    return [*_first_purchases(user_id, 5, category="shopping"), outlier], outlier


def _seed_zero_rule(user_id):
    history = _daily(user_id, 6)
    return history, history[-1]


@dataclass(frozen=True)
class Scenario:
    name: str
    seed: Callable[[int], tuple[list[Transaction], Transaction]]
    # In app.rules.engine.RULES order -- the order rule_names_by_transaction yields.
    expected_rules: tuple[str, ...]
    # Stated independently of graph.RULE_TOOL_NODES, so routing tests check
    # the mapping rather than restate it.
    expected_tools: frozenset[str]
    flagged_count: int


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario("velocity", _seed_velocity, ("velocity",), frozenset({"get_transaction_history"}), 6),
        Scenario("amount_deviation", _seed_amount_deviation, ("amount_deviation",), frozenset(), 1),
        Scenario("new_merchant_risk", _seed_new_merchant_risk, ("new_merchant_risk",),
                 frozenset({"get_merchant_risk_score"}), 1),
        Scenario("geographic_anomaly", _seed_geographic_anomaly, ("geographic_anomaly",),
                 frozenset({"get_geo_distance"}), 1),
        Scenario("meridian_triple", _seed_meridian_triple,
                 ("geographic_anomaly", "amount_deviation", "new_merchant_risk"),
                 frozenset({"get_geo_distance", "get_merchant_risk_score"}), 1),
        Scenario("zero_rule", _seed_zero_rule, (), frozenset(), 0),
    )
}
SINGLE_RULE_SCENARIOS = ("velocity", "amount_deviation", "new_merchant_risk", "geographic_anomaly")
FLAGGED_SCENARIOS = (*SINGLE_RULE_SCENARIOS, "meridian_triple")


@dataclass(frozen=True)
class SeededScenario:
    scenario: Scenario
    user_id: int
    primary_id: int
    flagged_ids: tuple[int, ...]
    rule_names_by_id: dict[int, list[str]]
    rule_values_by_id: dict[int, dict[str, dict]]
    # The primary transaction's own snapshot (transaction_data_from_orm).
    primary: dict


def seed_scenario(name: str) -> SeededScenario:
    """Seeds one scenario for a fresh user and self-checks it: every flagged
    transaction fired exactly scenario.expected_rules, and exactly
    scenario.flagged_count transactions are flagged."""
    scenario = SCENARIOS[name]
    db = SessionLocal()
    try:
        user = User(name=f"SCRUM-54 {name}")
        db.add(user)
        db.flush()
        transactions, primary = scenario.seed(user.id)
        db.add_all(transactions)
        db.commit()

        all_transactions = db.query(Transaction).filter(Transaction.user_id == user.id).all()
        hits = evaluate_all_rules(all_transactions)
        rule_names_by_id = rule_names_by_transaction(hits)
        rule_values_by_id = rule_values_by_transaction(hits)

        assert len(rule_names_by_id) == scenario.flagged_count, (name, rule_names_by_id)
        for transaction_id, rule_names in rule_names_by_id.items():
            assert tuple(rule_names) == scenario.expected_rules, (name, transaction_id, rule_names)
        if scenario.flagged_count:
            assert primary.id in rule_names_by_id, name

        return SeededScenario(
            scenario=scenario,
            user_id=user.id,
            primary_id=primary.id,
            flagged_ids=tuple(sorted(rule_names_by_id)),
            rule_names_by_id=rule_names_by_id,
            rule_values_by_id=rule_values_by_id,
            primary=transaction_data_from_orm(primary),
        )
    finally:
        db.close()


def investigation_state(seeded: SeededScenario, transaction_id: int | None = None) -> InvestigationState:
    """The state scripts.compose_rationales builds for one flagged transaction."""
    transaction_id = transaction_id or seeded.primary_id
    db = SessionLocal()
    try:
        transaction = transaction_data_from_orm(db.get(Transaction, transaction_id))
    finally:
        db.close()
    return {
        "transaction": transaction,
        "rule_names": seeded.rule_names_by_id.get(transaction_id, []),
        "rule_values": seeded.rule_values_by_id.get(transaction_id, {}),
        "evidence": {},
        "rationale": "",
        "composed_rationale": "",
    }


# --- Scripted completions ---------------------------------------------------
# Keyed by outcome, built per scenario: "grounded" cites only the flagged
# transaction's own merchant (citable from the snapshot for every rule type),
# so it passes validation whichever rule fired.

UNGROUNDED_TEXT = "This looks like it happened in Wakanda, which is unusual."
_LOWERCASE_FILLER = " the amount and location noted above were as expected for this account"


def grounded_text(seeded: SeededScenario) -> str:
    return f"Your purchase from {seeded.primary['merchant']} was flagged for review."


def completion(kind: str, seeded: SeededScenario) -> AIMessage:
    if kind == "grounded":
        return AIMessage(content=grounded_text(seeded))
    if kind == "ungrounded":
        return AIMessage(content=UNGROUNDED_TEXT)
    if kind == "too_long":
        # Grounded, padded with digit- and capital-free filler so the only
        # possible violation is the length cap.
        text = grounded_text(seeded)
        text += _LOWERCASE_FILLER * ((MAX_RATIONALE_CHARS - len(text)) // len(_LOWERCASE_FILLER) + 2)
        assert len(text) > MAX_RATIONALE_CHARS
        return AIMessage(content=text)
    if kind == "thinking":
        # A live model's extended-thinking response shape (SCRUM-53 regression).
        return AIMessage(content=[
            {"type": "thinking", "thinking": "internal reasoning", "signature": "c2lnbmF0dXJl"},
            {"type": "text", "text": grounded_text(seeded)},
        ])
    raise ValueError(kind)


_ANTHROPIC_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def anthropic_error(kind: str) -> anthropic.APIError:
    """Real anthropic SDK exceptions, as they'd reach compose_rationale once
    the SDK's own retries are exhausted. Constructed only, never raised by a
    real request (see tests/no_live_guard.py)."""
    if kind == "timeout":
        return anthropic.APITimeoutError(request=_ANTHROPIC_REQUEST)
    cls, status = {
        "server_error": (anthropic.InternalServerError, 500),
        "rate_limit": (anthropic.RateLimitError, 429),
        "overloaded": (anthropic.OverloadedError, 529),
    }[kind]
    response = httpx.Response(status, request=_ANTHROPIC_REQUEST)
    return cls(f"simulated {kind}", response=response, body=None)


class ScriptedLLM:
    """Replaces graph.get_chat_model (where compose_rationale looks it up) with
    a wrapper around the REAL llm.get_chat_model(responses=...), one scripted
    response per model call. An exception in the script is raised from inside
    the model's invoke(), exactly where a real SDK error would surface.

    `invocations` counts actual model.invoke() calls; `unscripted_calls`
    counts calls after the script ran out -- the scripted_llm fixture
    (tests/conftest.py) fails the test on any, since compose_rationale would
    otherwise quietly record them as a ModelError.
    """

    def __init__(self, responses):
        self._remaining = list(responses)
        self.invocations = 0
        self.unscripted_calls = 0

    def _one(self, response) -> Iterator[AIMessage]:
        self.invocations += 1  # runs on the model's next(), i.e. at invoke time
        if isinstance(response, BaseException):
            raise response
        yield response

    def get_chat_model(self):
        if not self._remaining:
            self.unscripted_calls += 1
            raise AssertionError("unscripted model call")
        return llm_module.get_chat_model(responses=self._one(self._remaining.pop(0)))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "ScriptedLLM":
        monkeypatch.setattr(graph_module, "get_chat_model", self.get_chat_model)
        return self

    @property
    def remaining(self) -> int:
        return len(self._remaining)
