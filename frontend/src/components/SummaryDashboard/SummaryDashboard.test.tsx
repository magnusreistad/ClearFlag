import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SummaryDashboard } from './SummaryDashboard'
import { fetchSummary } from '../../api/summary'
import type { SummaryResponse } from '../../types/summary'

vi.mock('../../api/summary', () => ({
  fetchSummary: vi.fn(),
}))

const mockedFetchSummary = vi.mocked(fetchSummary)

const NO_COUNTS = { confirmed_fraud: 0, false_positive: 0, ignored: 0, unreviewed: 0 }

// The backend's exact-number fixture (backend/tests/test_summary.py).
function makeSummary(overrides: Partial<SummaryResponse> = {}): SummaryResponse {
  return {
    flagged_count: 8,
    rule_hit_count: 9,
    dispositions: { confirmed_fraud: 1, false_positive: 3, ignored: 1, unreviewed: 3 },
    judged_count: 5,
    fp_rate: 0.75,
    coverage: 0.625,
    unflagged_dispositions: { confirmed_fraud: 0, false_positive: 0, ignored: 0 },
    trend: [
      { week_start: '2026-03-02', flagged: 0, ...NO_COUNTS },
      { week_start: '2026-03-09', flagged: 4, ...NO_COUNTS, confirmed_fraud: 1, false_positive: 3 },
      { week_start: '2026-03-16', flagged: 0, ...NO_COUNTS },
      { week_start: '2026-03-23', flagged: 4, ...NO_COUNTS, ignored: 1, unreviewed: 3 },
    ],
    ...overrides,
  }
}

function kpi(label: string): HTMLElement {
  return screen.getByText(label, { selector: 'dt' }).parentElement!
}

beforeEach(() => {
  mockedFetchSummary.mockReset()
})

describe('SummaryDashboard', () => {
  it('shows a loading state before data arrives', () => {
    mockedFetchSummary.mockReturnValue(new Promise(() => {}))
    render(<SummaryDashboard refreshKey={0} />)
    expect(screen.getByRole('status')).toHaveTextContent(/loading your summary/i)
  })

  it('shows an error state when the fetch fails, and retries on demand', async () => {
    mockedFetchSummary.mockRejectedValueOnce(new Error('/transactions/summary responded with 500'))
    render(<SummaryDashboard refreshKey={0} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/couldn't load your summary/i)
    expect(alert).toHaveTextContent('/transactions/summary responded with 500')

    mockedFetchSummary.mockResolvedValueOnce(makeSummary())
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText('5 of 8 judged')).toBeInTheDocument()
    expect(mockedFetchSummary).toHaveBeenCalledTimes(2)
  })

  it('formats the KPIs', async () => {
    mockedFetchSummary.mockResolvedValue(makeSummary())
    render(<SummaryDashboard refreshKey={0} />)
    await screen.findByText('5 of 8 judged')

    expect(within(kpi('Flagged')).getByText('8')).toBeInTheDocument()
    expect(within(kpi('Flagged')).getByText('9 rule hits')).toBeInTheDocument()
    expect(within(kpi('False-positive rate')).getByText('75%')).toBeInTheDocument()
    expect(within(kpi('False-positive rate')).getByText('3 of 4 decided')).toBeInTheDocument()
    expect(within(kpi('Coverage')).getByText('62.5%')).toBeInTheDocument()
    expect(within(kpi('Coverage')).getByText('5 of 8 judged')).toBeInTheDocument()
    expect(within(kpi('Unreviewed')).getByText('3')).toBeInTheDocument()
  })

  it('renders the chart with a legend and a text alternative for every week', async () => {
    mockedFetchSummary.mockResolvedValue(makeSummary())
    render(<SummaryDashboard refreshKey={0} />)

    const figure = await screen.findByRole('figure')
    expect(figure).toHaveAccessibleName(
      'Flagged transactions per week, Mar 2 to Mar 23: 8 flagged transactions across 4 weeks, ' +
        'peaking at 4 in the week of Mar 9. Each bar is split by current review status.',
    )
    for (const label of ['Confirmed fraud', 'False positive', 'Ignored', 'Unreviewed']) {
      expect(within(figure).getByRole('columnheader', { name: label })).toBeInTheDocument()
    }
    const table = within(figure).getByRole('table', { name: /flagged transactions per week/i })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((row) => within(row).getAllByRole('cell').map((cell) => cell.textContent))).toEqual([
      ['0', '0', '0', '0', '0'],
      ['4', '1', '3', '0', '0'],
      ['0', '0', '0', '0', '0'],
      ['4', '0', '0', '1', '3'],
    ])
    expect(screen.queryByText(/no longer flagged/i)).not.toBeInTheDocument()
  })

  it('shows "—" and a caption when nothing has been judged yet', async () => {
    mockedFetchSummary.mockResolvedValue(
      makeSummary({
        dispositions: { ...NO_COUNTS, unreviewed: 8 },
        judged_count: 0,
        fp_rate: null,
        coverage: 0,
      }),
    )
    render(<SummaryDashboard refreshKey={0} />)
    await screen.findByText('0 of 8 judged')

    expect(within(kpi('False-positive rate')).getByText('—')).toBeInTheDocument()
    expect(within(kpi('False-positive rate')).getByText('No judgments yet')).toBeInTheDocument()
    expect(within(kpi('Coverage')).getByText('0%')).toBeInTheDocument()
    expect(screen.getByRole('figure')).toBeInTheDocument()
  })

  it('replaces the chart with a message when nothing is flagged', async () => {
    mockedFetchSummary.mockResolvedValue(
      makeSummary({
        flagged_count: 0,
        rule_hit_count: 0,
        dispositions: NO_COUNTS,
        judged_count: 0,
        fp_rate: null,
        coverage: null,
        trend: [{ week_start: '2026-03-02', flagged: 0, ...NO_COUNTS }],
      }),
    )
    render(<SummaryDashboard refreshKey={0} />)

    expect(await screen.findByText('No flagged transactions')).toBeInTheDocument()
    expect(screen.queryByRole('figure')).not.toBeInTheDocument()
    expect(within(kpi('Flagged')).getByText('0 rule hits')).toBeInTheDocument()
    expect(within(kpi('False-positive rate')).getByText('—')).toBeInTheDocument()
    expect(within(kpi('Coverage')).getByText('—')).toBeInTheDocument()
    expect(within(kpi('Coverage')).getByText('Nothing flagged')).toBeInTheDocument()
  })

  it('notes reviews on transactions that are no longer flagged, outside the KPIs', async () => {
    mockedFetchSummary.mockResolvedValue(
      makeSummary({ unflagged_dispositions: { confirmed_fraud: 0, false_positive: 1, ignored: 1 } }),
    )
    render(<SummaryDashboard refreshKey={0} />)

    expect(
      await screen.findByText(
        "2 reviews (1 false positive, 1 ignored) are on transactions that are no longer flagged, so they aren't counted above.",
      ),
    ).toBeInTheDocument()
  })

  it('refetches when refreshKey changes, keeping the old summary up meanwhile', async () => {
    mockedFetchSummary.mockResolvedValueOnce(makeSummary())
    const { rerender } = render(<SummaryDashboard refreshKey={0} />)
    await screen.findByText('5 of 8 judged')

    let resolve!: (summary: SummaryResponse) => void
    mockedFetchSummary.mockReturnValueOnce(new Promise((r) => (resolve = r)))
    rerender(<SummaryDashboard refreshKey={1} />)

    expect(screen.getByText('5 of 8 judged')).toBeInTheDocument()
    expect(screen.queryByText(/loading your summary/i)).not.toBeInTheDocument()
    resolve(makeSummary({ judged_count: 6, coverage: 0.75 }))
    expect(await screen.findByText('6 of 8 judged')).toBeInTheDocument()
    expect(mockedFetchSummary).toHaveBeenCalledTimes(2)
  })
})
