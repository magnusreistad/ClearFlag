import { describe, expect, it } from 'vitest'
import { splitRationale } from './rationale'

describe('splitRationale', () => {
  it('returns a single-item array for a single-rule rationale', () => {
    expect(splitRationale('Flagged: this amount is 200% higher than your typical spend in this category.')).toEqual([
      'this amount is 200% higher than your typical spend in this category.',
    ])
  })

  it('splits a multi-rule rationale into one item per rule', () => {
    const rationale =
      'Flagged: this amount is 1162% higher than your typical spend in this category. ' +
      'Flagged: this is your first purchase from this merchant, and the amount is 562% higher than your typical first-time purchase.'

    expect(splitRationale(rationale)).toEqual([
      'this amount is 1162% higher than your typical spend in this category.',
      'this is your first purchase from this merchant, and the amount is 562% higher than your typical first-time purchase.',
    ])
  })

  it('returns the whole trimmed text as one item when there is no "Flagged: " marker', () => {
    expect(splitRationale('  This $340.00 charge was flagged for two reasons.  ')).toEqual([
      'This $340.00 charge was flagged for two reasons.',
    ])
  })

  it('returns an empty array for an empty string', () => {
    expect(splitRationale('')).toEqual([])
  })
})
