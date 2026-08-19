const currencyFormatter = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
const groupHeadingFormatter = new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'short', day: 'numeric' })
const timeFormatter = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' })

// Every row in this dataset is spending (no income/refund type field yet),
// so amounts are rendered as outflows: "-$42.30".
export function formatCurrency(amount: string): string {
  return currencyFormatter.format(-Number(amount))
}

export function formatTime(isoTimestamp: string): string {
  return timeFormatter.format(new Date(isoTimestamp))
}

function dateKeyFromDate(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

export function dateKey(isoTimestamp: string): string {
  return dateKeyFromDate(new Date(isoTimestamp))
}

export function formatGroupHeading(key: string): string {
  const today = dateKeyFromDate(new Date())
  const yesterdayDate = new Date()
  yesterdayDate.setDate(yesterdayDate.getDate() - 1)
  const yesterday = dateKeyFromDate(yesterdayDate)

  if (key === today) return 'Today'
  if (key === yesterday) return 'Yesterday'

  const [year, month, day] = key.split('-').map(Number)
  return groupHeadingFormatter.format(new Date(year, month - 1, day))
}

export function formatCategory(category: string): string {
  return category.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}
