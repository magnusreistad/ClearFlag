from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.database import SessionLocal as TestingSessionLocal
from app.investigation_agent import graph as graph_module
from app.investigation_agent.graph import (
    KM_PER_MILE,
    get_geo_distance,
    get_merchant_risk_score,
    get_transaction_history,
    investigation_graph,
    plan_tool_calls,
)
from app.investigation_agent.state import InvestigationState, TransactionData
from app.models import Transaction, User
from app.rules.geographic_anomaly import (
    _centroid,
    _haversine_miles,
    evaluate_geographic_anomaly,
)
from scripts.seed_transactions import generate_triple_rule_fraud


def _transaction_data(transaction, *, id_: int, user_id: int) -> TransactionData:
    return {
        "id": id_,
        "user_id": user_id,
        "timestamp": transaction.timestamp,
        "merchant": transaction.merchant,
        "category": transaction.category,
        "amount": transaction.amount,
        "latitude": transaction.latitude,
        "longitude": transaction.longitude,
        "location_label": transaction.location_label,
    }


def _make_transaction(
    user_id: int,
    timestamp: datetime,
    *,
    merchant: str = "Test Merchant",
    amount: Decimal = Decimal("10.00"),
    latitude: float = 47.6062,
    longitude: float = -122.3321,
    location_label: str = "Seattle, WA",
) -> Transaction:
    return Transaction(
        user_id=user_id,
        timestamp=timestamp,
        merchant=merchant,
        category="groceries",
        amount=amount,
        latitude=latitude,
        longitude=longitude,
        location_label=location_label,
    )


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
    assert result["tool_errors"] == {}
    # compose_rationale/validate (SCRUM-53) still run for every path, including this one -- but
    # this state has no rule_values, so get_chat_model()'s default mock response (which cites
    # nothing) is what validate sees, and it's discarded here for an unrelated reason: the
    # bracketed "[MOCK]" token trips the SCRUM-53 bare-entity check. Either way the composed
    # rationale never survives validation with this deliberately underspecified state, which is
    # exactly what this test (about tool routing, not composition) wants: an empty final
    # rationale and rationale_source="interim", not a crash.
    assert result["rationale"] == ""
    assert result["rationale_source"] == "interim"


def test_velocity_only_flag_visits_get_transaction_history_branch():
    """Covers the one tool branch the SCRUM-65 transaction can never reach
    (get_transaction_history, mapped from velocity): that seed transaction
    deliberately excludes velocity, since it requires a 6+ transaction
    burst that doesn't compose with a single planted transaction. A
    separate synthetic single-rule case exercises this branch instead.
    """
    anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state: InvestigationState = {
        "transaction": {
            "id": 1,
            "user_id": 1,
            "timestamp": anchor,
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
    assert result["tool_errors"] == {}


def test_amount_deviation_only_flag_has_no_tool_and_routes_straight_to_collect_evidence():
    """amount_deviation maps to no tool (SCRUM-51) -- its evidence already
    comes from the rules engine, not a tool call, so a flag triggered by
    only that rule should reach collect_evidence with no evidence gathered
    rather than being routed nowhere. plan_tool_calls still emits an
    AIMessage (with an empty tool_calls list); tools_condition reads that
    and routes straight past the "tools" node to collect_evidence --
    confirmed here by there being exactly one message (plan_tool_calls's)
    and zero ToolMessages, proving ToolNode never ran. See
    test_investigation_agent_compose.py's TestNoToolCompositionStillRuns for
    this same no-tool path continuing on into compose_rationale/validate.
    """
    state: InvestigationState = {
        "transaction": {
            "id": 2,
            "user_id": 1,
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
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
    assert result["tool_errors"] == {}
    assert len(result["messages"]) == 1
    assert result["messages"][0].tool_calls == []


class TestPlanToolCalls:
    """SCRUM-51: direct unit tests of plan_tool_calls, isolating the
    deterministic-planning step (which tool_calls get built) from the
    routing/execution tests above (which cover the graph reaching the
    right branches end to end).
    """

    def test_builds_one_tool_call_per_mapped_rule_with_deterministic_ids(self):
        state: InvestigationState = {
            "transaction": {
                "id": 42,
                "user_id": 7,
                "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "merchant": "Meridian Duty-Free Traders",
                "category": "shopping",
                "amount": Decimal("500.00"),
                "latitude": 14.5995,
                "longitude": 120.9842,
                "location_label": "Manila, Philippines",
            },
            "rule_names": ["new_merchant_risk", "amount_deviation", "geographic_anomaly"],
            "evidence": {},
            "rationale": "",
        }

        result = plan_tool_calls(state)

        [ai_message] = result["messages"]
        names = {call["name"] for call in ai_message.tool_calls}
        assert names == {"get_merchant_risk_score", "get_geo_distance"}
        ids = {call["id"] for call in ai_message.tool_calls}
        # Derived from tool name + transaction id, not a random uuid -- stable
        # across repeated runs of the same transaction.
        assert ids == {"get_merchant_risk_score-42", "get_geo_distance-42"}

    def test_amount_deviation_only_produces_zero_tool_calls(self):
        state: InvestigationState = {
            "transaction": {
                "id": 2,
                "user_id": 1,
                "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
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

        result = plan_tool_calls(state)

        [ai_message] = result["messages"]
        assert ai_message.tool_calls == []


def test_tool_exception_is_isolated_and_recorded_in_tool_errors(monkeypatch):
    """SCRUM-51 groundwork for SCRUM-56 (not the fallback itself): ToolNode
    is configured with handle_tool_errors=True, so a tool raising becomes an
    error ToolMessage instead of crashing the graph. collect_evidence
    records that in tool_errors (keyed by tool name) and leaves the failed
    tool's evidence key absent entirely -- never a substitute value. The
    OTHER tool call in the same parallel batch (get_merchant_risk_score)
    must still complete and populate its own evidence key, proving the two
    parallel tool failures/successes are isolated from each other.
    """
    start_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    [(seed_transaction, _)] = generate_triple_rule_fraud(user_id=1, start_date=start_date)

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated get_geo_distance failure")

    monkeypatch.setattr(graph_module.get_geo_distance, "func", _raise)

    state: InvestigationState = {
        "transaction": _transaction_data(seed_transaction, id_=999, user_id=1),
        "rule_names": ["new_merchant_risk", "amount_deviation", "geographic_anomaly"],
        "evidence": {},
        "rationale": "",
    }

    result = investigation_graph.invoke(state)

    assert set(result["evidence"].keys()) == {"get_merchant_risk_score"}
    assert result["evidence"]["get_merchant_risk_score"]["merchant"] == "Meridian Duty-Free Traders"
    assert "get_geo_distance" not in result["evidence"]
    assert "get_geo_distance" in result["tool_errors"]
    assert "simulated get_geo_distance failure" in result["tool_errors"]["get_geo_distance"]


class TestGetTransactionHistory:
    """SCRUM-48/51: direct unit tests of the get_transaction_history tool's
    underlying function (via .func, bypassing the Runnable/tool_call
    envelope), against a real (Postgres) DB session, independent of
    the graph's routing -- the routing tests above already cover that this
    tool gets called for a velocity-only flag.
    """

    def _seed_user_and_anchor(self, db, *, anchor: datetime) -> tuple[User, Transaction]:
        user = User(name="Test User")
        db.add(user)
        db.flush()
        anchor_transaction = _make_transaction(user.id, anchor, merchant="Anchor Merchant", amount=Decimal("25.00"))
        db.add(anchor_transaction)
        db.flush()
        return user, anchor_transaction

    def test_transactions_inside_window_are_returned_with_correct_count(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user, _ = self._seed_user_and_anchor(db, anchor=anchor)
            # 4 minutes before the anchor, inside the 10-minute window.
            inside = _make_transaction(
                user.id, anchor - timedelta(minutes=4), merchant="Corner Store", amount=Decimal("5.00")
            )
            # Exactly at the window boundary (diff == window_minutes) -- velocity.py's
            # own `> window` exclusion test keeps this one in, so this tool should too.
            boundary = _make_transaction(
                user.id, anchor - timedelta(minutes=10), merchant="Boundary Shop", amount=Decimal("7.50")
            )
            db.add_all([inside, boundary])
            db.commit()

            _content, evidence = get_transaction_history.func(user_id=user.id, anchor_timestamp=anchor)
        finally:
            db.close()

        assert evidence["user_id"] == user.id
        assert evidence["count"] == 3
        merchants = {t["merchant"] for t in evidence["transactions"]}
        assert merchants == {"Anchor Merchant", "Corner Store", "Boundary Shop"}

    def test_transactions_outside_window_are_excluded(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user, _ = self._seed_user_and_anchor(db, anchor=anchor)
            # 11 minutes before the anchor -- just outside the 10-minute window.
            outside = _make_transaction(
                user.id, anchor - timedelta(minutes=11), merchant="Too Early Shop", amount=Decimal("15.00")
            )
            # After the anchor -- the window only looks backward from it.
            after = _make_transaction(
                user.id, anchor + timedelta(minutes=1), merchant="Too Late Shop", amount=Decimal("12.00")
            )
            db.add_all([outside, after])
            db.commit()

            _content, evidence = get_transaction_history.func(user_id=user.id, anchor_timestamp=anchor)
        finally:
            db.close()

        assert evidence["count"] == 1
        assert [t["merchant"] for t in evidence["transactions"]] == ["Anchor Merchant"]

    def test_user_with_no_matching_transactions_returns_empty_history(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            other_user = User(name="Other User")
            db.add(other_user)
            db.flush()
            # Belongs to a different user -- must not leak into this user's history.
            db.add(_make_transaction(other_user.id, anchor, merchant="Someone Else's Purchase"))
            db.commit()

            _content, evidence = get_transaction_history.func(user_id=999, anchor_timestamp=anchor)
        finally:
            db.close()

        assert evidence["transactions"] == []
        assert evidence["count"] == 0


class TestGetMerchantRiskScore:
    """SCRUM-49/51: direct unit tests of the get_merchant_risk_score tool's
    underlying function (via .func), against a real (Postgres) DB
    session, independent of the graph's routing -- the SCRUM-65 routing
    test above already covers that this tool gets called for a
    new_merchant_risk flag.
    """

    def test_true_first_time_transaction_is_flagged_first_with_zero_prior_count(self):
        """A user with real history at *other* merchants, but none yet at
        this one -- proves the count is scoped by merchant, not just user.
        """
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user = User(name="Test User")
            db.add(user)
            db.flush()
            other_merchant_history = _make_transaction(
                user.id, anchor - timedelta(days=1), merchant="Corner Store", amount=Decimal("5.00")
            )
            db.add(other_merchant_history)
            db.commit()

            _content, evidence = get_merchant_risk_score.func(
                user_id=user.id, merchant="New Boutique", anchor_timestamp=anchor
            )
        finally:
            db.close()

        assert evidence["is_first_transaction"] is True
        assert evidence["prior_transaction_count"] == 0
        assert evidence["risk_tier"] is None

    def test_repeat_merchant_transaction_is_not_first_with_correct_prior_count(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user = User(name="Test User")
            db.add(user)
            db.flush()
            first_purchase = _make_transaction(
                user.id, anchor - timedelta(days=2), merchant="Regular Cafe", amount=Decimal("6.00")
            )
            second_purchase = _make_transaction(
                user.id, anchor - timedelta(days=1), merchant="Regular Cafe", amount=Decimal("6.50")
            )
            db.add_all([first_purchase, second_purchase])
            db.commit()

            _content, evidence = get_merchant_risk_score.func(
                user_id=user.id, merchant="Regular Cafe", anchor_timestamp=anchor
            )
        finally:
            db.close()

        assert evidence["is_first_transaction"] is False
        assert evidence["prior_transaction_count"] == 2

    def test_user_merchant_pair_with_no_history_at_all_is_treated_as_first(self):
        """Mirrors get_transaction_history's empty-history test: a
        user/merchant pair with nothing persisted for it at all still
        resolves cleanly rather than erroring.
        """
        _content, evidence = get_merchant_risk_score.func(
            user_id=999,
            merchant="Nonexistent Merchant",
            anchor_timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )

        assert evidence["is_first_transaction"] is True
        assert evidence["prior_transaction_count"] == 0
        assert evidence["risk_tier"] is None


class TestGetGeoDistance:
    """SCRUM-50/51: direct unit tests of the get_geo_distance tool's
    underlying function (via .func), against a real (Postgres) DB
    session, independent of the graph's routing (the SCRUM-65 routing test
    above already covers this tool getting called for a geographic_anomaly
    flag).
    """

    def _seed_user_and_history(self, db, *, anchor: datetime, count: int, **location_kwargs) -> tuple[User, list]:
        user = User(name="Test User")
        db.add(user)
        db.flush()
        history = [
            _make_transaction(user.id, anchor - timedelta(days=count - i), **location_kwargs)
            for i in range(count)
        ]
        db.add_all(history)
        db.commit()
        return user, history

    def test_distance_matches_geographic_anomalys_own_computation(self):
        """Seeds a tight Seattle-area prior history (matching the rule's
        MIN_HISTORY_COUNT floor exactly) and uses the SCRUM-65 Meridian
        Duty-Free Traders transaction (Manila) as the flagged one. Rather
        than hand-computing an independent expected distance (which could
        just as easily drift from the rule as a from-scratch tool
        implementation would), this asserts the tool's output against
        _centroid/_haversine_miles -- the exact functions both the rule and
        the tool now share -- and separately confirms the rule itself
        actually flags this history+transaction pair as anomalous, so the
        two can't silently disagree about what "anomalous" means here.
        """
        start_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        [(seed_transaction, _)] = generate_triple_rule_fraud(user_id=1, start_date=start_date)

        db = TestingSessionLocal()
        try:
            user, history = self._seed_user_and_history(db, anchor=seed_transaction.timestamp, count=5)

            _content, evidence = get_geo_distance.func(
                user_id=user.id,
                latitude=seed_transaction.latitude,
                longitude=seed_transaction.longitude,
                anchor_timestamp=seed_transaction.timestamp,
            )

            expected_lat, expected_lon = _centroid(history)
            expected_miles = _haversine_miles(
                expected_lat, expected_lon, seed_transaction.latitude, seed_transaction.longitude
            )
            rule_hits = evaluate_geographic_anomaly([*history, seed_transaction])
        finally:
            db.close()

        assert evidence["user_id"] == user.id
        assert evidence["latitude"] == seed_transaction.latitude
        assert evidence["longitude"] == seed_transaction.longitude
        assert evidence["distance_miles"] == pytest.approx(expected_miles)
        assert len(rule_hits) == 1  # sanity: the rule agrees this is anomalous

    def test_distance_km_is_consistent_with_distance_miles(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user, _ = self._seed_user_and_history(db, anchor=anchor, count=5)

            _content, evidence = get_geo_distance.func(
                user_id=user.id, latitude=14.5995, longitude=120.9842, anchor_timestamp=anchor
            )
        finally:
            db.close()

        assert evidence["distance_miles"] is not None
        assert evidence["distance_km"] == pytest.approx(evidence["distance_miles"] * KM_PER_MILE)

    def test_insufficient_history_returns_none_with_no_fabricated_values(self):
        """One short of MIN_HISTORY_COUNT (5) -- the same floor the rule
        itself requires before computing a centroid -- so the tool must not
        fabricate a distance or label from too small a sample.
        """
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user, _ = self._seed_user_and_history(db, anchor=anchor, count=4)

            _content, evidence = get_geo_distance.func(
                user_id=user.id, latitude=14.5995, longitude=120.9842, anchor_timestamp=anchor
            )
        finally:
            db.close()

        assert evidence["distance_km"] is None
        assert evidence["distance_miles"] is None
        assert evidence["typical_location_label"] is None

    def test_user_with_no_history_at_all_returns_none_with_no_fabricated_values(self):
        _content, evidence = get_geo_distance.func(
            user_id=999,
            latitude=14.5995,
            longitude=120.9842,
            anchor_timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )

        assert evidence["distance_km"] is None
        assert evidence["distance_miles"] is None
        assert evidence["typical_location_label"] is None

    def test_typical_location_label_is_the_most_frequent_prior_label(self):
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user = User(name="Test User")
            db.add(user)
            db.flush()
            labels = ["Seattle, WA", "Seattle, WA", "Seattle, WA", "Portland, OR", "Portland, OR"]
            history = [
                _make_transaction(user.id, anchor - timedelta(days=len(labels) - i), location_label=label)
                for i, label in enumerate(labels)
            ]
            db.add_all(history)
            db.commit()

            _content, evidence = get_geo_distance.func(
                user_id=user.id, latitude=14.5995, longitude=120.9842, anchor_timestamp=anchor
            )
        finally:
            db.close()

        assert evidence["typical_location_label"] == "Seattle, WA"

    def test_typical_location_label_tie_is_broken_alphabetically(self):
        """Two labels tied at 2 occurrences each, one at 1 -- "Bellevue, WA"
        sorts before "Seattle, WA", so it wins the tie (see
        _most_common_location_label's min()-based tiebreak).
        """
        anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db = TestingSessionLocal()
        try:
            user = User(name="Test User")
            db.add(user)
            db.flush()
            labels = ["Bellevue, WA", "Bellevue, WA", "Seattle, WA", "Seattle, WA", "Portland, OR"]
            history = [
                _make_transaction(user.id, anchor - timedelta(days=len(labels) - i), location_label=label)
                for i, label in enumerate(labels)
            ]
            db.add_all(history)
            db.commit()

            _content, evidence = get_geo_distance.func(
                user_id=user.id, latitude=14.5995, longitude=120.9842, anchor_timestamp=anchor
            )
        finally:
            db.close()

        assert evidence["typical_location_label"] == "Bellevue, WA"
