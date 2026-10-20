import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(cleanup)

// jsdom has no ResizeObserver, which Recharts' ResponsiveContainer needs
// (SCRUM-32). A no-op is enough: tests never resize, and the container
// renders at its initialDimension.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub
