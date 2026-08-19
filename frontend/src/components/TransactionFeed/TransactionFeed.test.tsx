import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TransactionFeed } from './TransactionFeed'
import { fetchTransactions } from '../../api/transactions'
import type { Transaction, TransactionListResponse } from '../../types/transaction'

vi.mock('../../api/transactions', () => ({
  fetchTransactions: vi.fn(),
}))

const mockedFetchTransactions = vi.mocked(fetchTransactions)

function makeTransaction(overrides: Partial<Transaction> = {}): Transaction {
  return {
    id: 1,
    user_id: 2,
    timestamp: '2026-08-17T14:30:00Z',
    merchant: 'Whole Foods Market',
    category: 'groceries',
    amount: '54.12',
    latitude: 47.6062,
    longitude: -122.3321,
    location_label: 'Seattle, WA',
    ...overrides,
  }
}

function makeResponse(items: Transaction[], overrides: Partial<TransactionListResponse> = {}): TransactionListResponse {
  return { items, total: items.length, limit: 50, offset: 0, ...overrides }
}

beforeEach(() => {
  mockedFetchTransactions.mockReset()
})

describe('TransactionFeed', () => {
  it('shows a loading state before data arrives', () => {
    mockedFetchTransactions.mockReturnValue(new Promise(() => {}))
    render(<TransactionFeed />)
    expect(screen.getByText(/loading your transactions/i)).toBeInTheDocument()
  })

  it('renders merchant, amount, category, and location once loaded', async () => {
    mockedFetchTransactions.mockResolvedValue(makeResponse([makeTransaction()]))
    render(<TransactionFeed />)

    expect(await screen.findByText('Whole Foods Market')).toBeInTheDocument()
    expect(screen.getByText('-$54.12')).toBeInTheDocument()
    expect(screen.getByText(/Groceries/)).toBeInTheDocument()
    expect(screen.getByText(/Seattle, WA/)).toBeInTheDocument()
  })

  it('shows an empty state when there are no transactions', async () => {
    mockedFetchTransactions.mockResolvedValue(makeResponse([]))
    render(<TransactionFeed />)

    expect(await screen.findByText(/no transactions yet/i)).toBeInTheDocument()
  })

  it('shows an error state when the fetch fails, and retries on demand', async () => {
    mockedFetchTransactions.mockRejectedValueOnce(new Error('/transactions responded with 500'))
    render(<TransactionFeed />)

    expect(await screen.findByText(/couldn't load your transactions/i)).toBeInTheDocument()

    mockedFetchTransactions.mockResolvedValueOnce(makeResponse([makeTransaction()]))
    await userEvent.click(screen.getByRole('button', { name: /try again/i }))

    expect(await screen.findByText('Whole Foods Market')).toBeInTheDocument()
  })

  it('shows a Load more button when more transactions exist, and fetches the next page', async () => {
    mockedFetchTransactions.mockResolvedValueOnce(
      makeResponse([makeTransaction({ id: 1 })], { total: 2 }),
    )
    render(<TransactionFeed />)

    const loadMoreButton = await screen.findByRole('button', { name: /load more/i })

    mockedFetchTransactions.mockResolvedValueOnce(
      makeResponse([makeTransaction({ id: 2, merchant: 'Trader Joe\'s' })], { total: 2, offset: 1 }),
    )
    await userEvent.click(loadMoreButton)

    expect(await screen.findByText("Trader Joe's")).toBeInTheDocument()
    expect(mockedFetchTransactions).toHaveBeenLastCalledWith({ limit: 50, offset: 1 })
    expect(screen.queryByRole('button', { name: /load more/i })).not.toBeInTheDocument()
  })
})
