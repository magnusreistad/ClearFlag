import threading

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Transaction, TransactionFlag
from app.rules.engine import (
    concatenate_rationales,
    evaluate_all_rules,
    rule_names_by_transaction,
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
    """
    with _flags_refresh_lock:
        all_transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
        hits = evaluate_all_rules(all_transactions)

        transaction_ids = [t.id for t in all_transactions]
        db.query(TransactionFlag).filter(TransactionFlag.transaction_id.in_(transaction_ids)).delete(
            synchronize_session=False
        )
        db.add_all(
            TransactionFlag(transaction_id=hit.transaction_id, rule_name=hit.rule_name, rationale=hit.rationale)
            for hit in hits
        )
        db.commit()

        return concatenate_rationales(hits), rule_names_by_transaction(hits)


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
