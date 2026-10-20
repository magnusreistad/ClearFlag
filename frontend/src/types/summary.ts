// GET /transactions/summary (SCRUM-32). Mirrors backend/app/schemas.py.

// Currently flagged transactions by their latest disposition; the four
// counts sum to flagged_count.
export interface DispositionCounts {
  confirmed_fraud: number
  false_positive: number
  ignored: number
  unreviewed: number
}

// Latest dispositions on transactions that are no longer flagged - kept
// out of fp_rate and coverage.
export interface UnflaggedDispositionCounts {
  confirmed_fraud: number
  false_positive: number
  ignored: number
}

// One ISO week (Monday start, UTC). week_start is a plain 'YYYY-MM-DD' date.
export interface TrendBucket extends DispositionCounts {
  week_start: string
  flagged: number
}

export interface SummaryResponse {
  flagged_count: number
  rule_hit_count: number
  dispositions: DispositionCounts
  judged_count: number
  // null when there are no confirmed_fraud or false_positive judgments.
  fp_rate: number | null
  // null when nothing is flagged.
  coverage: number | null
  unflagged_dispositions: UnflaggedDispositionCounts
  // Every week from the first transaction to the last, zero-filled.
  trend: TrendBucket[]
}
