import threading

from fastapi import APIRouter, Depends, Query
from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.database import get_db
from app.investigation_agent.fingerprint import compute_fact_fingerprint
from app.investigation_agent.payload import build_payload
from app.investigation_agent.prompts import PROMPT_VERSION
from app.investigation_agent.state import TransactionData, transaction_data_from_orm
from app.models import AgentRationale, Transaction, TransactionFlag
from app.rules.engine import (
    concatenate_rationales,
    evaluate_all_rules,
    rule_names_by_transaction,
    rule_values_by_transaction,
)
from app.schemas import TransactionListResponse, TransactionOut

router = APIRouter()

# Serializes _refresh_flags so two concurrent requests (e.g. React
# StrictMode double-firing the mount effect in dev, or overlapping browser
# tabs) can't race between the delete and reinsert below and violate the
# (transaction_id, rule_name) unique constraint. FastAPI runs sync
# endpoints in a threadpool, so concurrent requests genuinely run on
# separate threads. A single process-wide lock (rather than per-user) is
# fine at this project's scale.
_flags_refresh_lock = threading.Lock()


def _lookup_agent_rationales(
    db: Session,
    flagged_transaction_data: dict[int, TransactionData],
    rule_names_by_id: dict[int, list[str]],
    rule_values_by_id: dict[int, dict[str, dict]],
) -> dict[int, str]:
    """SCRUM-53 Phase B. Read-only lookup against agent_rationales for the
    flagged transactions in `flagged_transaction_data` -- no tool calls, no
    model calls, no writes (composition only ever happens in
    scripts.compose_rationales, never on this request path). One query
    covers every flagged transaction at once (no N+1): each transaction's
    CURRENT fact fingerprint is matched against validation_passed=true rows
    for PROMPT_VERSION. A transaction missing from the returned dict --
    whether that's a cache miss, a failed-only row, or a stale
    prompt_version -- gets exactly the same treatment from the caller: fall
    back to the interim formatter's rationale, unchanged.
    """
    fingerprint_by_id = {
        transaction_id: compute_fact_fingerprint(
            build_payload(transaction_data, rule_names_by_id[transaction_id], rule_values_by_id.get(transaction_id, {}))
        )
        for transaction_id, transaction_data in flagged_transaction_data.items()
    }
    if not fingerprint_by_id:
        return {}

    rows = (
        db.query(AgentRationale)
        .filter(
            tuple_(AgentRationale.transaction_id, AgentRationale.fact_fingerprint).in_(
                list(fingerprint_by_id.items())
            ),
            AgentRationale.prompt_version == PROMPT_VERSION,
            AgentRationale.validation_passed.is_(True),
        )
        .all()
    )

    latest_by_id: dict[int, AgentRationale] = {}
    for row in rows:
        current = latest_by_id.get(row.transaction_id)
        if current is None or row.created_at > current.created_at:
            latest_by_id[row.transaction_id] = row

    return {transaction_id: row.rationale for transaction_id, row in latest_by_id.items() if row.rationale}


def _refresh_flags(db: Session, user_id: int) -> tuple[dict[int, str], dict[int, list[str]]]:
    """Re-run the rules engine over a user's full transaction history and
    replace their persisted flags with the fresh result.

    SCRUM-61 MVP tradeoff: none of the four rules are incremental (each
    needs the user's full history to evaluate correctly), and this GET
    endpoint is the only write trigger this schema has, so every call
    recomputes and persists flags for the user's entire history, not just
    the requested page. That's an unusual side effect for a GET, but fine
    at this project's synthetic-data scale -- a real system would move this
    to a background job instead of a request-time recompute.

    SCRUM-53 Phase B: the agent_rationales lookup below is deliberately
    OUTSIDE the `with _flags_refresh_lock` block -- it's a read against a
    different table, doesn't participate in the TransactionFlag
    delete/reinsert this lock protects, and doesn't need to be serialized
    against a concurrent request. `flagged_transaction_data` is captured
    *inside* the lock, before db.commit() expires `all_transactions`'
    attributes, specifically so the lookup can run afterward without
    triggering a lazy-refresh SELECT per transaction.
    """
    with _flags_refresh_lock:
        all_transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
        hits = evaluate_all_rules(all_transactions)
        rule_names_by_id = rule_names_by_transaction(hits)
        rule_values_by_id = rule_values_by_transaction(hits)
        transactions_by_id = {t.id: t for t in all_transactions}
        flagged_transaction_data = {
            transaction_id: transaction_data_from_orm(transactions_by_id[transaction_id])
            for transaction_id in rule_names_by_id
        }

        transaction_ids = [t.id for t in all_transactions]
        db.query(TransactionFlag).filter(TransactionFlag.transaction_id.in_(transaction_ids)).delete(
            synchronize_session=False
        )
        db.add_all(
            TransactionFlag(transaction_id=hit.transaction_id, rule_name=hit.rule_name, rationale=hit.rationale)
            for hit in hits
        )
        db.commit()

        interim_rationale_by_id = concatenate_rationales(hits)

    agent_rationale_by_id = _lookup_agent_rationales(
        db, flagged_transaction_data, rule_names_by_id, rule_values_by_id
    )

    return {**interim_rationale_by_id, **agent_rationale_by_id}, rule_names_by_id


def _to_transaction_out(
    t: Transaction, rationale_by_id: dict[int, str], rule_names_by_id: dict[int, list[str]]
) -> TransactionOut:
    rationale = rationale_by_id.get(t.id)
    return TransactionOut(
        id=t.id,
        user_id=t.user_id,
        timestamp=t.timestamp,
        merchant=t.merchant,
        category=t.category,
        amount=t.amount,
        latitude=t.latitude,
        longitude=t.longitude,
        location_label=t.location_label,
        is_flagged=rationale is not None,
        rationale=rationale,
        rule_names=rule_names_by_id.get(t.id, []),
    )


@router.get("/transactions", response_model=TransactionListResponse)
def list_transactions(
    user_id: int = Query(..., description="Scope results to this user's transactions"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> TransactionListResponse:
    rationale_by_id, rule_names_by_id = _refresh_flags(db, user_id)

    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    total = query.count()
    transactions = (
        query.order_by(Transaction.timestamp.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return TransactionListResponse(
        items=[_to_transaction_out(t, rationale_by_id, rule_names_by_id) for t in transactions],
        total=total,
        limit=limit,
        offset=offset,
    )
