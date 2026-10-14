import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TransactionFeed } from './TransactionFeed'
import { ApiError } from '../../api/client'
import { fetchTransactions, postDisposition } from '../../api/transactions'
import type { Transaction, TransactionListResponse } from '../../types/transaction'

vi.mock('../../api/transactions', () => ({
  fetchTransactions: vi.fn(),
  postDisposition: vi.fn(),
}))

const mockedFetchTransactions = vi.mocked(fetchTransactions)
const mockedPostDisposition = vi.mocked(postDisposition)

const FLAGGED = {
  is_flagged: true,
  rationale: 'Flagged: This amount is 200% higher than your typical spend in this category.',
  rule_names: ['amount_deviation'],
}

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
    is_flagged: false,
    rationale: null,
    rule_names: [],
    disposition: null,
    ...overrides,
  }
}

function makeResponse(items: Transaction[], overrides: Partial<TransactionListResponse> = {}): TransactionListResponse {
  return { items, total: items.length, limit: 50, offset: 0, ...overrides }
}

beforeEach(() => {
  mockedFetchTransactions.mockReset()
  mockedPostDisposition.mockReset()
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

  it('renders no flag badge for a flagged transaction with an empty rationale', async () => {
    mockedFetchTransactions.mockResolvedValue(
      makeResponse([makeTransaction({ is_flagged: true, rationale: '', rule_names: ['amount_deviation'] })]),
    )
    render(<TransactionFeed />)

    expect(await screen.findByText('Whole Foods Market')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /flagged/i })).not.toBeInTheDocument()
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

  describe('dispositions (SCRUM-30)', () => {
    it('shows a saved disposition in the row without reloading the feed', async () => {
      mockedFetchTransactions.mockResolvedValue(makeResponse([makeTransaction({ id: 7, ...FLAGGED })]))
      mockedPostDisposition.mockResolvedValue({
        id: 1,
        transaction_id: 7,
        disposition: 'false_positive',
        note: 'Known merchant.',
        rule_names: ['amount_deviation'],
        created_at: '2026-10-14T09:15:00Z',
      })
      render(<TransactionFeed />)

      await userEvent.click(await screen.findByRole('button', { name: 'Review' }))
      await userEvent.click(screen.getByRole('radio', { name: 'False positive' }))
      await userEvent.type(screen.getByRole('textbox', { name: 'Note (optional)' }), 'Known merchant.')
      await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

      expect(await screen.findByText('Review saved.')).toBeInTheDocument()
      const row = screen.getByRole('listitem')
      expect(within(row).getAllByRole('status')[0]).toHaveTextContent('False positive')
      expect(within(row).queryByText('Not reviewed')).not.toBeInTheDocument()
      expect(within(row).getByText('Known merchant.')).toBeInTheDocument()
      expect(mockedFetchTransactions).toHaveBeenCalledTimes(1)
    })

    it('re-judging posts a new disposition and replaces the one shown', async () => {
      mockedFetchTransactions.mockResolvedValue(
        makeResponse([
          makeTransaction({
            id: 7,
            ...FLAGGED,
            disposition: { value: 'confirmed_fraud', note: 'First look.', created_at: '2026-10-13T09:00:00Z' },
          }),
        ]),
      )
      mockedPostDisposition.mockResolvedValue({
        id: 2,
        transaction_id: 7,
        disposition: 'ignored',
        note: null,
        rule_names: ['amount_deviation'],
        created_at: '2026-10-14T09:15:00Z',
      })
      render(<TransactionFeed />)

      expect(await screen.findByText('Confirmed fraud')).toBeInTheDocument()
      await userEvent.click(screen.getByRole('button', { name: 'Review' }))
      expect(screen.getByRole('group', { name: 'Change your review' })).toBeInTheDocument()
      await userEvent.click(screen.getByRole('radio', { name: 'Ignore' }))
      await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

      expect(mockedPostDisposition).toHaveBeenCalledExactlyOnceWith(7, { disposition: 'ignored', note: null })
      expect(await screen.findByText('Ignored')).toBeInTheDocument()
      expect(screen.queryByText('Confirmed fraud')).not.toBeInTheDocument()
      expect(screen.queryByText('First look.')).not.toBeInTheDocument()
    })

    it('shows "Not reviewed" with controls on a flagged row with no disposition', async () => {
      mockedFetchTransactions.mockResolvedValue(makeResponse([makeTransaction({ ...FLAGGED })]))
      render(<TransactionFeed />)

      expect(await screen.findByText('Not reviewed')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Review' })).toBeInTheDocument()
    })

    it('still offers controls on a flagged row with an empty rationale', async () => {
      mockedFetchTransactions.mockResolvedValue(
        makeResponse([makeTransaction({ is_flagged: true, rationale: '', rule_names: ['amount_deviation'] })]),
      )
      render(<TransactionFeed />)

      expect(await screen.findByText('Not reviewed')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Review' })).toBeInTheDocument()
    })

    it('shows a no-longer-flagged row\'s disposition read-only, with no controls', async () => {
      mockedFetchTransactions.mockResolvedValue(
        makeResponse([
          makeTransaction({
            disposition: { value: 'false_positive', note: 'Regular gym charge.', created_at: '2026-10-14T09:15:00Z' },
          }),
        ]),
      )
      render(<TransactionFeed />)

      expect(await screen.findByText('False positive')).toBeInTheDocument()
      expect(screen.getByText('Regular gym charge.')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Review' })).not.toBeInTheDocument()
      expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    })

    it('shows no review line on a row that was never flagged or judged', async () => {
      mockedFetchTransactions.mockResolvedValue(makeResponse([makeTransaction()]))
      render(<TransactionFeed />)

      expect(await screen.findByText('Whole Foods Market')).toBeInTheDocument()
      expect(screen.queryByText('Not reviewed')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Review' })).not.toBeInTheDocument()
    })

    it('shows the error card and no rows - never "Not reviewed" - when the GET 500s', async () => {
      mockedFetchTransactions.mockRejectedValue(new ApiError('/transactions responded with 500', 500, null))
      render(<TransactionFeed />)

      expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't load your transactions/i)
      expect(screen.queryByRole('listitem')).not.toBeInTheDocument()
      expect(screen.queryByText('Not reviewed')).not.toBeInTheDocument()
    })
  })
})
