import { describe, expect, it } from 'vitest'
import { formatCategory, formatCurrency, formatGroupHeading, dateKey } from './format'

describe('formatCurrency', () => {
  it('formats a decimal string as a negative USD amount', () => {
    expect(formatCurrency('42.30')).toBe('-$42.30')
  })

  it('rounds to two decimal places', () => {
    expect(formatCurrency('9.5')).toBe('-$9.50')
  })
})

describe('formatCategory', () => {
  it('turns a snake_case category into title case', () => {
    expect(formatCategory('gas_transport')).toBe('Gas Transport')
  })

  it('title-cases a single-word category', () => {
    expect(formatCategory('groceries')).toBe('Groceries')
  })
})

describe('formatGroupHeading', () => {
  it('labels the current date as Today', () => {
    const today = dateKey(new Date().toISOString())
    expect(formatGroupHeading(today)).toBe('Today')
  })

  it('labels a date far in the past with weekday, month, and day', () => {
    expect(formatGroupHeading('2020-01-15')).toBe('Wednesday, Jan 15')
  })
})
