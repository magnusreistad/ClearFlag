import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { postDisposition } from '../../api/transactions'
import type { CurrentDisposition, DispositionOut } from '../../types/transaction'
import { DispositionPanel, NOTE_MAX_LENGTH } from './DispositionPanel'

vi.mock('../../api/transactions', () => ({
  postDisposition: vi.fn(),
}))

const mockedPostDisposition = vi.mocked(postDisposition)

function makeSaved(overrides: Partial<DispositionOut> = {}): DispositionOut {
  return {
    id: 1,
    transaction_id: 7,
    disposition: 'false_positive',
    note: null,
    rule_names: ['amount_deviation'],
    created_at: '2026-10-14T09:15:00Z',
    ...overrides,
  }
}

function renderPanel(props: { isFlagged?: boolean; disposition?: CurrentDisposition | null } = {}) {
  const onSaved = vi.fn()
  render(
    <DispositionPanel
      transactionId={7}
      isFlagged={props.isFlagged ?? true}
      disposition={props.disposition ?? null}
      onSaved={onSaved}
    />,
  )
  return { onSaved }
}

async function openAndChoose(label: RegExp) {
  await userEvent.click(screen.getByRole('button', { name: 'Review' }))
  await userEvent.click(screen.getByRole('radio', { name: label }))
}

beforeEach(() => {
  mockedPostDisposition.mockReset()
})

describe('DispositionPanel', () => {
  it('shows "Not reviewed" and a collapsed Review toggle for an unreviewed flagged row', () => {
    renderPanel()

    expect(screen.getByRole('status')).toHaveTextContent('Not reviewed')
    const toggle = screen.getByRole('button', { name: 'Review' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
  })

  it('expands a labelled radio group, note, and Save button that the toggle controls', async () => {
    renderPanel()
    const toggle = screen.getByRole('button', { name: 'Review' })

    await userEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const form = document.getElementById(toggle.getAttribute('aria-controls')!)
    expect(form).toBeVisible()
    expect(screen.getByRole('group', { name: 'Review this flag' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Confirm fraud' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'False positive' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Ignore' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Note (optional)' })).toHaveAttribute('maxLength', String(NOTE_MAX_LENGTH))
    expect(screen.getByRole('button', { name: 'Save review' })).toBeDisabled()
  })

  it('shows an existing disposition with its note and time', () => {
    renderPanel({
      disposition: { value: 'confirmed_fraud', note: 'Called the customer.', created_at: '2026-10-14T09:15:00Z' },
    })

    expect(screen.getByRole('status')).toHaveTextContent('Confirmed fraud')
    expect(screen.getByText('Called the customer.')).toBeInTheDocument()
    expect(screen.getByRole('status').querySelector('time')).toHaveAttribute('dateTime', '2026-10-14T09:15:00Z')
  })

  it('posts the chosen disposition and note, then reports the saved disposition', async () => {
    mockedPostDisposition.mockResolvedValue(makeSaved({ disposition: 'false_positive', note: 'Known merchant.' }))
    const { onSaved } = renderPanel()

    await openAndChoose(/false positive/i)
    await userEvent.type(screen.getByRole('textbox', { name: 'Note (optional)' }), 'Known merchant.')
    await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

    expect(mockedPostDisposition).toHaveBeenCalledExactlyOnceWith(7, { disposition: 'false_positive', note: 'Known merchant.' })
    expect(onSaved).toHaveBeenCalledWith({
      value: 'false_positive',
      note: 'Known merchant.',
      created_at: '2026-10-14T09:15:00Z',
    })
    expect(await screen.findByText('Review saved.')).toHaveAttribute('role', 'status')
    expect(screen.getByRole('textbox', { name: 'Note (optional)' })).toHaveValue('')
  })

  it('sends an empty note as null', async () => {
    mockedPostDisposition.mockResolvedValue(makeSaved({ disposition: 'ignored' }))
    renderPanel()

    await openAndChoose(/ignore/i)
    await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

    expect(mockedPostDisposition).toHaveBeenCalledWith(7, { disposition: 'ignored', note: null })
  })

  it('disables the fieldset while a save is in flight, so a double-click sends exactly one POST', async () => {
    let resolvePost!: (value: DispositionOut) => void
    mockedPostDisposition.mockReturnValue(new Promise((resolve) => (resolvePost = resolve)))
    renderPanel()

    await openAndChoose(/confirm fraud/i)
    await userEvent.dblClick(screen.getByRole('button', { name: 'Save review' }))

    expect(mockedPostDisposition).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('group')).toBeDisabled()
    expect(screen.getByRole('radio', { name: 'Ignore' })).toBeDisabled()
    expect(screen.getByRole('textbox', { name: 'Note (optional)' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled()

    resolvePost(makeSaved({ disposition: 'confirmed_fraud' }))
    expect(await screen.findByText('Review saved.')).toBeInTheDocument()
    expect(screen.getByRole('group')).toBeEnabled()
  })

  it.each([
    [404, null, "This transaction couldn't be found."],
    [409, 'Transaction has no active flags to disposition', 'This transaction is no longer flagged. Refresh the page to see its current status.'],
    [500, null, "Couldn't save, try again."],
  ])('shows an inline error for a %i', async (status, detail, message) => {
    mockedPostDisposition.mockRejectedValue(new ApiError(`responded with ${status}`, status, detail))
    const { onSaved } = renderPanel()

    await openAndChoose(/confirm fraud/i)
    await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(onSaved).not.toHaveBeenCalled()
    expect(screen.getByRole('group')).toBeEnabled()
  })

  it("shows a 422's server message beside the note", async () => {
    mockedPostDisposition.mockRejectedValue(
      new ApiError('responded with 422', 422, 'String should have at most 2000 characters'),
    )
    renderPanel()

    await openAndChoose(/confirm fraud/i)
    await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('String should have at most 2000 characters')
    const noteField = screen.getByRole('textbox', { name: 'Note (optional)' })
    expect(noteField).toHaveAccessibleDescription(expect.stringContaining('String should have at most 2000 characters'))
    expect(noteField).toHaveAttribute('aria-invalid', 'true')
  })

  it('shows the generic error for a network failure', async () => {
    mockedPostDisposition.mockRejectedValue(new TypeError('Failed to fetch'))
    renderPanel()

    await openAndChoose(/confirm fraud/i)
    await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't save, try again.")
  })

  it('counts note characters live', async () => {
    renderPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Review' }))

    expect(screen.getByText(`0 / ${NOTE_MAX_LENGTH}`)).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox', { name: 'Note (optional)' }), 'Hello')
    expect(screen.getByText(`5 / ${NOTE_MAX_LENGTH}`)).toBeInTheDocument()
  })

  it('refuses to submit a note over the limit without calling the API', async () => {
    renderPanel()
    await openAndChoose(/confirm fraud/i)
    // fireEvent sets the value directly, which maxLength doesn't stop.
    fireEvent.change(screen.getByRole('textbox', { name: 'Note (optional)' }), {
      target: { value: 'x'.repeat(NOTE_MAX_LENGTH + 1) },
    })

    await userEvent.click(screen.getByRole('button', { name: 'Save review' }))

    expect(screen.getByRole('alert')).toHaveTextContent(`Notes can be at most ${NOTE_MAX_LENGTH} characters.`)
    expect(mockedPostDisposition).not.toHaveBeenCalled()
  })

  it('shows a disposition read-only, with no controls, once the row is no longer flagged', () => {
    renderPanel({
      isFlagged: false,
      disposition: { value: 'false_positive', note: 'Regular gym charge.', created_at: '2026-10-14T09:15:00Z' },
    })

    expect(screen.getByRole('status')).toHaveTextContent('False positive')
    expect(screen.getByText('Regular gym charge.')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('renders nothing for a row that is neither flagged nor dispositioned', () => {
    const { container } = render(
      <DispositionPanel transactionId={7} isFlagged={false} disposition={null} onSaved={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('is fully keyboard-operable: tab to Review, arrow between options, submit', async () => {
    mockedPostDisposition.mockResolvedValue(makeSaved({ disposition: 'false_positive' }))
    const user = userEvent.setup()
    renderPanel()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Review' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('button', { name: 'Review' })).toHaveAttribute('aria-expanded', 'true')

    await user.tab()
    expect(screen.getByRole('radio', { name: 'Confirm fraud' })).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('radio', { name: 'False positive' })).toHaveFocus()
    expect(screen.getByRole('radio', { name: 'False positive' })).toBeChecked()

    await user.tab() // the note
    await user.tab() // Save review
    expect(screen.getByRole('button', { name: 'Save review' })).toHaveFocus()
    await user.keyboard('{Enter}')

    expect(mockedPostDisposition).toHaveBeenCalledExactlyOnceWith(7, { disposition: 'false_positive', note: null })
  })
})
