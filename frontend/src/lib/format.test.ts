import { describe, expect, it } from 'vitest'
import { formatCategory, formatCurrency, formatGroupHeading, formatRatio, formatWeek, dateKey } from './format'

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

describe('formatRatio', () => {
  it('formats a ratio as a percentage with at most one decimal', () => {
    expect(formatRatio(0.75)).toBe('75%')
    expect(formatRatio(0.625)).toBe('62.5%')
    expect(formatRatio(0)).toBe('0%')
  })

  it('shows an em dash for null, not 0%', () => {
    expect(formatRatio(null)).toBe('—')
  })
})

describe('formatWeek', () => {
  it('formats the UTC date, whatever the local time zone', () => {
    expect(formatWeek('2026-07-13')).toBe('Jul 13')
  })
})
