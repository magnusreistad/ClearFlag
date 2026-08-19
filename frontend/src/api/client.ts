const API_BASE_URL = import.meta.env.VITE_API_BASE_URL

export class ApiError extends Error {}

export async function apiGet<T>(path: string, params: Record<string, string | number>): Promise<T> {
  const url = new URL(path, API_BASE_URL)
  for (const [key, value] of Object.entries(params)) {
    url.searchParams.set(key, String(value))
  }

  const response = await fetch(url)
  if (!response.ok) {
    throw new ApiError(`${path} responded with ${response.status}`)
  }
  return (await response.json()) as T
}
