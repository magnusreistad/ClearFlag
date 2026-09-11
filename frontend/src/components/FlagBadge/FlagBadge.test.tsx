import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { FlagBadge } from './FlagBadge'

const SINGLE_RULE_RATIONALE = 'Flagged: this amount is 200% higher than your typical spend in this category.'

const MULTI_RULE_RATIONALE =
  'Flagged: this amount is 1162% higher than your typical spend in this category. ' +
  'Flagged: this is your first purchase from this merchant, and the amount is 562% higher than your typical first-time purchase.'

const THREE_RULE_RATIONALE =
  'Flagged: this amount is 1162% higher than your typical spend in this category. ' +
  'Flagged: this is your first purchase from this merchant, and the amount is 562% higher than your typical first-time purchase. ' +
  'Flagged: three purchases from this merchant occurred within the last hour.'

function ControlledFlagBadge({ rationale }: { rationale: string }) {
  const [isOpen, setIsOpen] = useState(false)
  return <FlagBadge rationale={rationale} isOpen={isOpen} onOpenChange={setIsOpen} />
}

describe('FlagBadge', () => {
  it('renders a Flagged badge that is collapsed by default', () => {
    render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} />)
    expect(screen.getByRole('button', { name: /flagged/i })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
  })

  it('renders a single-rule rationale as one clean line, not a list', async () => {
    render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    expect(screen.getByText('this amount is 200% higher than your typical spend in this category.')).toBeInTheDocument()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('renders a multi-rule rationale as a list of distinct reasons', async () => {
    render(<ControlledFlagBadge rationale={MULTI_RULE_RATIONALE} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('this amount is 1162% higher than your typical spend in this category.')
    expect(items[1]).toHaveTextContent(
      "this is your first purchase from this merchant, and the amount is 562% higher than your typical first-time purchase.",
    )
  })

  it('exposes the rule count via data-rule-count', () => {
    const { container, rerender } = render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} />)
    expect(container.querySelector('[data-rule-count]')).toHaveAttribute('data-rule-count', '1')

    rerender(<ControlledFlagBadge rationale={MULTI_RULE_RATIONALE} />)
    expect(container.querySelector('[data-rule-count]')).toHaveAttribute('data-rule-count', '2')

    rerender(<ControlledFlagBadge rationale={THREE_RULE_RATIONALE} />)
    expect(container.querySelector('[data-rule-count]')).toHaveAttribute('data-rule-count', '3')
  })

  // The severity treatment (SCRUM-26) is pure CSS keyed off data-rule-count
  // (see FlagBadge.module.css), so the seam worth testing at this level is
  // that the attribute lands with the right value at each tier boundary -
  // rendered style output isn't something these component tests assert on.
  it('advances past the single-rule tier for any rule count above one', () => {
    const { container, rerender } = render(<ControlledFlagBadge rationale={MULTI_RULE_RATIONALE} />)
    const badge = container.querySelector('[data-rule-count]')
    expect(badge).not.toHaveAttribute('data-rule-count', '1')

    rerender(<ControlledFlagBadge rationale={THREE_RULE_RATIONALE} />)
    expect(badge).not.toHaveAttribute('data-rule-count', '1')
    expect(badge).not.toHaveAttribute('data-rule-count', '2')
  })

  it('toggles the panel closed when the badge is clicked again', async () => {
    render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} />)
    const badge = screen.getByRole('button', { name: /flagged/i })

    await userEvent.click(badge)
    expect(screen.getByRole('region')).toBeInTheDocument()

    await userEvent.click(badge)
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
  })

  it('closes when Escape is pressed', async () => {
    render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))
    expect(screen.getByRole('region')).toBeInTheDocument()

    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
  })
})
