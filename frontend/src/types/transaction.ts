export type DispositionValue = 'confirmed_fraud' | 'false_positive' | 'ignored'

// A transaction's latest disposition (SCRUM-29). The backend appends a new
// row on every judgment; GET /transactions only ever carries the latest.
export interface CurrentDisposition {
  value: DispositionValue
  note: string | null
  created_at: string
}

export interface Transaction {
  id: number
  user_id: number
  timestamp: string
  merchant: string
  category: string
  // Serialized as a string by the backend (Pydantic Decimal), not a number.
  amount: string
  latitude: number
  longitude: number
  location_label: string
  is_flagged: boolean
  rationale: string | null
  rule_names: string[]
  // null when never dispositioned. Present even after the transaction stops
  // being flagged - the judgment still happened.
  disposition: CurrentDisposition | null
}

export interface TransactionListResponse {
  items: Transaction[]
  total: number
  limit: number
  offset: number
}

// 201 body of POST /transactions/{id}/disposition (SCRUM-29).
export interface DispositionOut {
  id: number
  transaction_id: number
  disposition: DispositionValue
  note: string | null
  rule_names: string[]
  created_at: string
}
