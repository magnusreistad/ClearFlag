// Non-2xx responses only. A network failure (server down, CORS, offline)
// is left as fetch's own TypeError, so callers can tell "the server said no"
// apart from "the server never answered".
export class ApiError extends Error {
  readonly status: number
  // FastAPI's `detail`, flattened to one human-readable message: the string
  // itself for an HTTPException (404/409), or the first validation error's
  // `msg` for a 422. null when the body carried neither.
  readonly detail: string | null

  constructor(message: string, status: number, detail: string | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type QueryParams = Record<string, string | number>

// Read per call rather than at import so tests can stub the env var.
function buildUrl(path: string, params: QueryParams): URL {
  const url = new URL(path, import.meta.env.VITE_API_BASE_URL)
  for (const [key, value] of Object.entries(params)) {
    url.searchParams.set(key, String(value))
  }
  return url
}

function parseDetail(body: unknown): string | null {
  if (typeof body !== 'object' || body === null || !('detail' in body)) return null
  const { detail } = body
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const first: unknown = detail[0]
    if (typeof first === 'object' && first !== null && 'msg' in first && typeof first.msg === 'string') {
      return first.msg
    }
  }
  return null
}

async function handleResponse<T>(path: string, response: Response): Promise<T> {
  if (!response.ok) {
    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      // Not JSON (e.g. a proxy's HTML error page) - no detail to surface.
    }
    throw new ApiError(`${path} responded with ${response.status}`, response.status, parseDetail(body))
  }
  return (await response.json()) as T
}

export async function apiGet<T>(path: string, params: QueryParams): Promise<T> {
  const response = await fetch(buildUrl(path, params))
  return handleResponse<T>(path, response)
}

export async function apiPost<T>(path: string, params: QueryParams, body: unknown): Promise<T> {
  const response = await fetch(buildUrl(path, params), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return handleResponse<T>(path, response)
}
