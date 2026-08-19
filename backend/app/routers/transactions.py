from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Transaction
from app.schemas import TransactionListResponse, TransactionOut

router = APIRouter()


@router.get("/transactions", response_model=TransactionListResponse)
def list_transactions(
    user_id: int = Query(..., description="Scope results to this user's transactions"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> TransactionListResponse:
    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    total = query.count()
    transactions = (
        query.order_by(Transaction.timestamp.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return TransactionListResponse(
        items=[TransactionOut.model_validate(t) for t in transactions],
        total=total,
        limit=limit,
        offset=offset,
    )
