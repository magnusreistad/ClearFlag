from datetime import datetime, timezone
from decimal import Decimal

from app.investigation_agent.graph import investigation_graph
from app.investigation_agent.state import InvestigationState, TransactionData
from scripts.seed_transactions import generate_triple_rule_fraud


def _transaction_data(transaction, *, id_: int, user_id: int) -> TransactionData:
    return {
        "id": id_,
        "user_id": user_id,
        "merchant": transaction.merchant,
        "category": transaction.category,
        "amount": transaction.amount,
        "latitude": transaction.latitude,
        "longitude": transaction.longitude,
        "location_label": transaction.location_label,
    }


def test_scrum_65_triple_rule_transaction_fans_out_to_its_two_mapped_tool_branches():
    """SCRUM-65's seed transaction (Meridian Duty-Free Traders, Manila) trips
    new_merchant_risk, amount_deviation, and geographic_anomaly -- velocity
    is deliberately excluded from that plant. Of the three triggered rules,
    only new_merchant_risk and geographic_anomaly map to a tool node
    (SCRUM-51: amount_deviation needs no tool -- its values already come
    from the rules engine's invocation payload). So this transaction should
    visit exactly get_merchant_risk_score and get_geo_distance.
    """
    start_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    [(seed_transaction, _)] = generate_triple_rule_fraud(user_id=1, start_date=start_date)

    state: InvestigationState = {
        "transaction": _transaction_data(seed_transaction, id_=999, user_id=1),
        "rule_names": ["new_merchant_risk", "amount_deviation", "geographic_anomaly"],
        "evidence": {},
        "rationale": "",
    }

    result = investigation_graph.invoke(state)

    assert set(result["evidence"].keys()) == {"get_merchant_risk_score", "get_geo_distance"}
    assert result["evidence"]["get_merchant_risk_score"]["merchant"] == "Meridian Duty-Free Traders"
    assert result["evidence"]["get_geo_distance"]["latitude"] == seed_transaction.latitude
    assert result["rationale"] == ""  # assemble_rationale is a no-op placeholder until SCRUM-53


def test_velocity_only_flag_visits_get_transaction_history_branch():
    """Covers the one tool branch the SCRUM-65 transaction can never reach
    (get_transaction_history, mapped from velocity): that seed transaction
    deliberately excludes velocity, since it requires a 6+ transaction
    burst that doesn't compose with a single planted transaction. A
    separate synthetic single-rule case exercises this branch instead.
    """
    state: InvestigationState = {
        "transaction": {
            "id": 1,
            "user_id": 1,
            "merchant": "Test Merchant",
            "category": "groceries",
            "amount": Decimal("10.00"),
            "latitude": 47.6062,
            "longitude": -122.3321,
            "location_label": "Seattle, WA",
        },
        "rule_names": ["velocity"],
        "evidence": {},
        "rationale": "",
    }

    result = investigation_graph.invoke(state)

    assert set(result["evidence"].keys()) == {"get_transaction_history"}


def test_amount_deviation_only_flag_has_no_tool_and_routes_straight_to_assemble():
    """amount_deviation maps to no tool (SCRUM-51) -- its evidence already
    comes from the rules engine, not a tool call, so a flag triggered by
    only that rule should reach assemble_rationale with no evidence
    gathered rather than being routed nowhere.
    """
    state: InvestigationState = {
        "transaction": {
            "id": 2,
            "user_id": 1,
            "merchant": "Test Merchant",
            "category": "shopping",
            "amount": Decimal("500.00"),
            "latitude": 47.6062,
            "longitude": -122.3321,
            "location_label": "Seattle, WA",
        },
        "rule_names": ["amount_deviation"],
        "evidence": {},
        "rationale": "",
    }

    result = investigation_graph.invoke(state)

    assert result["evidence"] == {}
