import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { fetchSummary } from './api/summary'
import { fetchTransactions, postDisposition } from './api/transactions'
import type { SummaryResponse } from './types/summary'

vi.mock('./api/summary', () => ({ fetchSummary: vi.fn() }))
vi.mock('./api/transactions', () => ({
  fetchTransactions: vi.fn(),
  postDisposition: vi.fn(),
}))

const mockedFetchSummary = vi.mocked(fetchSummary)
const mockedFetchTransactions = vi.mocked(fetchTransactions)
const mockedPostDisposition = vi.mocked(postDisposition)

const SUMMARY: SummaryResponse = {
  flagged_count: 1,
  rule_hit_count: 1,
  dispositions: { confirmed_fraud: 0, false_positive: 0, ignored: 0, unreviewed: 1 },
  judged_count: 0,
  fp_rate: null,
  coverage: 0,
  unflagged_dispositions: { confirmed_fraud: 0, false_positive: 0, ignored: 0 },
  trend: [
    { week_start: '2026-08-17', flagged: 1, confirmed_fraud: 0, false_positive: 0, ignored: 0, unreviewed: 1 },
  ],
}

beforeEach(() => {
  mockedFetchSummary.mockReset()
  mockedFetchTransactions.mockReset()
  mockedPostDisposition.mockReset()
})

describe('App', () => {
  it('refetches the summary after a disposition is saved', async () => {
    mockedFetchSummary.mockResolvedValueOnce(SUMMARY).mockResolvedValueOnce({
      ...SUMMARY,
      dispositions: { ...SUMMARY.dispositions, false_positive: 1, unreviewed: 0 },
      judged_count: 1,
      fp_rate: 1,
      coverage: 1,
    })
    mockedFetchTransactions.mockResolvedValue({
      items: [
        {
          id: 7,
          user_id: 2,
          timestamp: '2026-08-17T14:30:00Z',
          merchant: 'Whole Foods Market',
          category: 'groceries',
          amount: '540.00',
          latitude: 47.6062,
          longitude: -122.3321,
          location_label: 'Seattle, WA',
          is_flagged: true,
          rationale: 'Flagged: This amount is 200% higher than your typical spend in this category.',
          rule_names: ['amount_deviation'],
          disposition: null,
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    })
    mockedPostDisposition.mockResolvedValue({
      id: 1,
      transaction_id: 7,
      disposition: 'false_positive',
      note: null,
      rule_names: ['amount_deviation'],
      created_at: '2026-10-20T09:15:00Z',
    })
    render(<App />)

    expect(await screen.findByText('0 of 1 judged')).toBeInTheDocument()
    expect(mockedFetchSummary).toHaveBeenCalledTimes(1)

    await userEvent.click(await screen.findByRole('button', { name: 'Review' }))
    await userEvent.click(screen.getByRole('radio', { name: 'False positive' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

    expect(await screen.findByText('1 of 1 judged')).toBeInTheDocument()
    expect(mockedFetchSummary).toHaveBeenCalledTimes(2)
  })
})
