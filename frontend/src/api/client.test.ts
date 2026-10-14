import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet, apiPost } from './client'

const fetchMock = vi.fn<typeof fetch>()

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

async function captureError(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise
  } catch (err) {
    return err
  }
  throw new Error('expected the promise to reject')
}

beforeEach(() => {
  vi.stubEnv('VITE_API_BASE_URL', 'http://api.test')
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockReset()
})

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('apiGet', () => {
  it('builds the URL from the base URL and query params', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }))

    await expect(apiGet('/transactions', { user_id: 2, limit: 50 })).resolves.toEqual({ ok: true })

    const url = fetchMock.mock.calls[0][0] as URL
    expect(url.toString()).toBe('http://api.test/transactions?user_id=2&limit=50')
  })

  it('throws an ApiError carrying the status and string detail', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: 'Transaction not found' }))

    const err = await captureError(apiGet('/transactions', {}))

    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 404, detail: 'Transaction not found' })
  })
})

describe('apiPost', () => {
  it('sends a JSON body with the query params on the URL', async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, { id: 9 }))

    await expect(apiPost('/transactions/5/disposition', { user_id: 2 }, { disposition: 'ignored' })).resolves.toEqual({
      id: 9,
    })

    const [url, init] = fetchMock.mock.calls[0]
    expect((url as URL).toString()).toBe('http://api.test/transactions/5/disposition?user_id=2')
    expect(init).toMatchObject({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disposition: 'ignored' }),
    })
  })

  it('parses a string detail (404)', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: 'Transaction not found' }))

    const err = await captureError(apiPost('/x', {}, {}))

    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 404, detail: 'Transaction not found' })
  })

  it('parses a string detail (409)', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: 'Transaction has no active flags to disposition' }))

    const err = await captureError(apiPost('/x', {}, {}))

    expect(err).toMatchObject({ status: 409, detail: 'Transaction has no active flags to disposition' })
  })

  it("parses a validation list (422) to its first error's message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        detail: [
          { type: 'string_too_long', loc: ['body', 'note'], msg: 'String should have at most 2000 characters' },
          { type: 'literal_error', loc: ['body', 'disposition'], msg: 'Input should be ...' },
        ],
      }),
    )

    const err = await captureError(apiPost('/x', {}, {}))

    expect(err).toMatchObject({ status: 422, detail: 'String should have at most 2000 characters' })
  })

  it('leaves detail null when the error body is not JSON (500)', async () => {
    fetchMock.mockResolvedValue(new Response('Internal Server Error', { status: 500 }))

    const err = await captureError(apiPost('/x', {}, {}))

    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 500, detail: null })
  })

  it('leaves a network failure as a TypeError, not an ApiError', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    const err = await captureError(apiPost('/x', {}, {}))

    expect(err).toBeInstanceOf(TypeError)
    expect(err).not.toBeInstanceOf(ApiError)
  })
})
