import { apiGet } from './client'
import type { SummaryResponse } from '../types/summary'

const SEEDED_USER_ID = import.meta.env.VITE_SEEDED_USER_ID

// SCRUM-32. The backend refreshes the user's flags before aggregating, so
// this reflects the same flag set GET /transactions shows.
export function fetchSummary(): Promise<SummaryResponse> {
  return apiGet<SummaryResponse>('/transactions/summary', { user_id: SEEDED_USER_ID })
}
