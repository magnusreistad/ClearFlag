import { apiGet, apiPost } from './client'
import type { DispositionOut, DispositionValue, TransactionListResponse } from '../types/transaction'

const SEEDED_USER_ID = import.meta.env.VITE_SEEDED_USER_ID

export function fetchTransactions(params: { limit: number; offset: number }): Promise<TransactionListResponse> {
  return apiGet<TransactionListResponse>('/transactions', {
    user_id: SEEDED_USER_ID,
    limit: params.limit,
    offset: params.offset,
  })
}

// Appends a new disposition row (SCRUM-29) - the backend never edits an
// earlier one, so re-judging a transaction is just another call to this.
export function postDisposition(
  transactionId: number,
  body: { disposition: DispositionValue; note: string | null },
): Promise<DispositionOut> {
  return apiPost<DispositionOut>(`/transactions/${transactionId}/disposition`, { user_id: SEEDED_USER_ID }, body)
}
