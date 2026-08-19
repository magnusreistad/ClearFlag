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
}

export interface TransactionListResponse {
  items: Transaction[]
  total: number
  limit: number
  offset: number
}
