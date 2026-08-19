import { useCallback, useEffect, useState } from 'react'
import { fetchTransactions } from '../../api/transactions'
import type { Transaction } from '../../types/transaction'
import { dateKey, formatCategory, formatCurrency, formatGroupHeading, formatTime } from '../../lib/format'
import styles from './TransactionFeed.module.css'

const PAGE_SIZE = 50

type Status = 'loading' | 'error' | 'ready'

interface DayGroup {
  key: string
  heading: string
  items: Transaction[]
}

// Relies on the backend returning items sorted newest-first, so grouping by
// day preserves that order without a separate sort here.
function groupByDay(items: Transaction[]): DayGroup[] {
  const groups: DayGroup[] = []
  const indexByKey = new Map<string, number>()

  for (const item of items) {
    const key = dateKey(item.timestamp)
    const existingIndex = indexByKey.get(key)
    if (existingIndex === undefined) {
      indexByKey.set(key, groups.length)
      groups.push({ key, heading: formatGroupHeading(key), items: [item] })
    } else {
      groups[existingIndex].items.push(item)
    }
  }

  return groups
}

export function TransactionFeed() {
  const [items, setItems] = useState<Transaction[]>([])
  const [total, setTotal] = useState<number | null>(null)
  const [status, setStatus] = useState<Status>('loading')
  const [error, setError] = useState<string | null>(null)
  const [isLoadingMore, setIsLoadingMore] = useState(false)

  const loadPage = useCallback(async (offset: number) => {
    const response = await fetchTransactions({ limit: PAGE_SIZE, offset })
    setItems((prev) => (offset === 0 ? response.items : [...prev, ...response.items]))
    setTotal(response.total)
  }, [])

  const loadInitial = useCallback(() => {
    setStatus('loading')
    setError(null)
    loadPage(0)
      .then(() => setStatus('ready'))
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : 'Something went wrong.')
        setStatus('error')
      })
  }, [loadPage])

  useEffect(() => {
    loadInitial()
  }, [loadInitial])

  const handleLoadMore = async () => {
    setIsLoadingMore(true)
    try {
      await loadPage(items.length)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong.')
    } finally {
      setIsLoadingMore(false)
    }
  }

  if (status === 'loading') {
    return (
      <div className={styles.feed}>
        <div className={styles.stateCard} role="status" aria-live="polite">
          <div className={styles.spinner} aria-hidden="true" />
          <p>Loading your transactions…</p>
        </div>
      </div>
    )
  }

  if (status === 'error') {
    return (
      <div className={styles.feed}>
        <div className={styles.stateCard} role="alert">
          <p className={styles.stateTitle}>Couldn't load your transactions</p>
          <p className={styles.stateBody}>{error}</p>
          <button type="button" className={styles.retryButton} onClick={loadInitial}>
            Try again
          </button>
        </div>
      </div>
    )
  }

  if (items.length === 0) {
    return (
      <div className={styles.feed}>
        <div className={styles.stateCard}>
          <p className={styles.stateTitle}>No transactions yet</p>
          <p className={styles.stateBody}>Once you make a purchase, it'll show up here.</p>
        </div>
      </div>
    )
  }

  const groups = groupByDay(items)
  const hasMore = total !== null && items.length < total

  return (
    <div className={styles.feed}>
      <header className={styles.header}>
        <h1 className={styles.title}>Transactions</h1>
        <p className={styles.subtitle}>
          {total} transaction{total === 1 ? '' : 's'}
        </p>
      </header>

      {groups.map((group) => (
        <section key={group.key} className={styles.group}>
          <h2 className={styles.groupHeading}>{group.heading}</h2>
          <ul className={styles.list}>
            {group.items.map((transaction) => (
              <li key={transaction.id} className={styles.row}>
                <div className={styles.avatar} aria-hidden="true">
                  {transaction.merchant.charAt(0)}
                </div>
                <div className={styles.details}>
                  <span className={styles.merchant}>{transaction.merchant}</span>
                  <span className={styles.meta}>
                    {formatCategory(transaction.category)} · {transaction.location_label}
                  </span>
                </div>
                <div className={styles.trailing}>
                  <span className={styles.amount}>{formatCurrency(transaction.amount)}</span>
                  <span className={styles.time}>{formatTime(transaction.timestamp)}</span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}

      {hasMore && (
        <button type="button" className={styles.loadMoreButton} onClick={handleLoadMore} disabled={isLoadingMore}>
          {isLoadingMore ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  )
}
