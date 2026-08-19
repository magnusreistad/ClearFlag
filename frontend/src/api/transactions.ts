import { apiGet } from './client'
import type { TransactionListResponse } from '../types/transaction'

const SEEDED_USER_ID = import.meta.env.VITE_SEEDED_USER_ID

export function fetchTransactions(params: { limit: number; offset: number }): Promise<TransactionListResponse> {
  return apiGet<TransactionListResponse>('/transactions', {
    user_id: SEEDED_USER_ID,
    limit: params.limit,
    offset: params.offset,
  })
}
