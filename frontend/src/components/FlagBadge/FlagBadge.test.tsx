import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { FlagBadge } from './FlagBadge'

const SINGLE_RULE_RATIONALE = 'Flagged: This amount is 200% higher than your typical spend in this category.'
const SINGLE_RULE_NAMES = ['amount_deviation']

const MULTI_RULE_RATIONALE =
  'Flagged: This amount is 1162% higher than your typical spend in this category. ' +
  'Flagged: This is your first purchase from this merchant, and the amount is 562% higher than your typical first-time purchase.'
const MULTI_RULE_NAMES = ['amount_deviation', 'new_merchant_risk']

const THREE_RULE_RATIONALE =
  'Flagged: This amount is 1162% higher than your typical spend in this category. ' +
  'Flagged: This is your first purchase from this merchant, and the amount is 562% higher than your typical first-time purchase. ' +
  'Flagged: Three purchases from this merchant occurred within the last hour.'
const THREE_RULE_NAMES = ['amount_deviation', 'new_merchant_risk', 'velocity']

// Investigation Agent rationales (SCRUM-53) are one composed paragraph with
// no "Flagged: " markers, whatever the rule count. Taken from the dev DB.
const AGENT_SINGLE_RULE_RATIONALE =
  'This $380.00 charge at CVS Pharmacy in Seattle, WA was flagged because it is far larger than what you typically ' +
  'spend on health purchases. Your typical spend in that category is about $41, so this charge is roughly 816% ' +
  'above your average. A jump that large from your usual pattern is what triggered the flag.'

const AGENT_TWO_RULE_RATIONALE =
  "This $340.00 charge at Riverside Wellness Spa was flagged for two reasons. It's about 1162% above your typical " +
  "spend of $27 in the health category. It's also your first purchase at this merchant, and it's about 562% higher " +
  'than the $51 you usually spend on a first-time purchase at a new merchant.'

const AGENT_THREE_RULE_RATIONALE =
  'This $3,200.00 purchase at Meridian Duty-Free Traders in Manila, Philippines was flagged for three reasons. It ' +
  'took place about 6,751 miles from Seattle, WA, your typical location, while your purchases usually happen within ' +
  'about 209 miles of there. The amount is also 1,873% above your typical shopping spend of $162. ' +
  "It's your first purchase at this merchant too, and first purchases at a new merchant typically run about $108, " +
  'so this one is 2,867% higher.'
const AGENT_THREE_RULE_NAMES = ['geographic_anomaly', 'amount_deviation', 'new_merchant_risk']

function ControlledFlagBadge({ rationale, ruleNames }: { rationale: string; ruleNames: string[] }) {
  const [isOpen, setIsOpen] = useState(false)
  return <FlagBadge rationale={rationale} ruleNames={ruleNames} isOpen={isOpen} onOpenChange={setIsOpen} />
}

describe('FlagBadge', () => {
  it('renders a Flagged badge that is collapsed by default', () => {
    render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} ruleNames={SINGLE_RULE_NAMES} />)
    expect(screen.getByRole('button', { name: /flagged/i })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
  })

  it('renders a single-rule rationale as one clean line, not a list', async () => {
    render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} ruleNames={SINGLE_RULE_NAMES} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    expect(screen.getByText('This amount is 200% higher than your typical spend in this category.')).toBeInTheDocument()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('renders a multi-rule rationale as a list of distinct reasons', async () => {
    render(<ControlledFlagBadge rationale={MULTI_RULE_RATIONALE} ruleNames={MULTI_RULE_NAMES} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('This amount is 1162% higher than your typical spend in this category.')
    expect(items[1]).toHaveTextContent(
      "This is your first purchase from this merchant, and the amount is 562% higher than your typical first-time purchase.",
    )
  })

  it('exposes the rule count via data-rule-count, from ruleNames rather than the rationale text', () => {
    const { container, rerender } = render(
      <ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} ruleNames={SINGLE_RULE_NAMES} />,
    )
    expect(container.querySelector('[data-rule-count]')).toHaveAttribute('data-rule-count', '1')

    rerender(<ControlledFlagBadge rationale={MULTI_RULE_RATIONALE} ruleNames={MULTI_RULE_NAMES} />)
    expect(container.querySelector('[data-rule-count]')).toHaveAttribute('data-rule-count', '2')

    rerender(<ControlledFlagBadge rationale={THREE_RULE_RATIONALE} ruleNames={THREE_RULE_NAMES} />)
    expect(container.querySelector('[data-rule-count]')).toHaveAttribute('data-rule-count', '3')
  })

  // The severity treatment (SCRUM-26) is pure CSS keyed off data-rule-count
  // (see FlagBadge.module.css), so the seam worth testing at this level is
  // that the attribute lands with the right value at each tier boundary -
  // rendered style output isn't something these component tests assert on.
  it('advances past the single-rule tier for any rule count above one', () => {
    const { container, rerender } = render(
      <ControlledFlagBadge rationale={MULTI_RULE_RATIONALE} ruleNames={MULTI_RULE_NAMES} />,
    )
    const badge = container.querySelector('[data-rule-count]')
    expect(badge).not.toHaveAttribute('data-rule-count', '1')

    rerender(<ControlledFlagBadge rationale={THREE_RULE_RATIONALE} ruleNames={THREE_RULE_NAMES} />)
    expect(badge).not.toHaveAttribute('data-rule-count', '1')
    expect(badge).not.toHaveAttribute('data-rule-count', '2')
  })

  it('renders an interim three-rule rationale as a list of three reasons', async () => {
    render(<ControlledFlagBadge rationale={THREE_RULE_RATIONALE} ruleNames={THREE_RULE_NAMES} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(items[2]).toHaveTextContent('Three purchases from this merchant occurred within the last hour.')
  })

  it('renders a single-rule agent rationale as one paragraph', async () => {
    render(<ControlledFlagBadge rationale={AGENT_SINGLE_RULE_RATIONALE} ruleNames={SINGLE_RULE_NAMES} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    expect(screen.getByText(AGENT_SINGLE_RULE_RATIONALE).tagName).toBe('P')
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  // SCRUM-57 regression: a multi-rule agent rationale is still one paragraph,
  // so it must not be wrapped in a one-item bulleted list.
  it.each([
    ['two', AGENT_TWO_RULE_RATIONALE, MULTI_RULE_NAMES],
    ['three', AGENT_THREE_RULE_RATIONALE, AGENT_THREE_RULE_NAMES],
  ])('renders a %s-rule agent rationale as one paragraph, not a list', async (_, rationale, ruleNames) => {
    render(<ControlledFlagBadge rationale={rationale} ruleNames={ruleNames} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    expect(screen.getByText(rationale).tagName).toBe('P')
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument()
  })

  it('keeps the severity tier on ruleNames for an agent rationale, not on its single paragraph', () => {
    const { container, rerender } = render(
      <ControlledFlagBadge rationale={AGENT_TWO_RULE_RATIONALE} ruleNames={MULTI_RULE_NAMES} />,
    )
    const badge = container.querySelector('[data-rule-count]')
    expect(badge).toHaveAttribute('data-rule-count', '2')

    rerender(<ControlledFlagBadge rationale={AGENT_THREE_RULE_RATIONALE} ruleNames={AGENT_THREE_RULE_NAMES} />)
    expect(badge).toHaveAttribute('data-rule-count', '3')
  })

  it('renders an agent rationale that opens with "Flagged: " as one paragraph', async () => {
    render(
      <ControlledFlagBadge
        rationale={`Flagged: ${AGENT_TWO_RULE_RATIONALE}`}
        ruleNames={MULTI_RULE_NAMES}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    expect(screen.getByText(AGENT_TWO_RULE_RATIONALE).tagName).toBe('P')
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('keeps an agent rationale with "Flagged: " mid-text as one whole paragraph', async () => {
    const rationale =
      'This $340.00 charge at Riverside Wellness Spa stood out. Flagged: it is about 1162% above your typical spend. ' +
      'Flagged: it is also your first purchase at this merchant.'
    render(<ControlledFlagBadge rationale={rationale} ruleNames={MULTI_RULE_NAMES} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))

    expect(screen.getByText(rationale).tagName).toBe('P')
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('toggles the panel closed when the badge is clicked again', async () => {
    render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} ruleNames={SINGLE_RULE_NAMES} />)
    const badge = screen.getByRole('button', { name: /flagged/i })

    await userEvent.click(badge)
    expect(screen.getByRole('region')).toBeInTheDocument()

    await userEvent.click(badge)
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
  })

  it('closes when Escape is pressed', async () => {
    render(<ControlledFlagBadge rationale={SINGLE_RULE_RATIONALE} ruleNames={SINGLE_RULE_NAMES} />)
    await userEvent.click(screen.getByRole('button', { name: /flagged/i }))
    expect(screen.getByRole('region')).toBeInTheDocument()

    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
  })
})
