/**
 * http.ts — the JSON request helpers every API client uses, and the error
 * they throw.
 *
 * Every request sends credentials:'include', so the httpOnly `sim_session`
 * cookie goes with it; the Vite dev proxy forwards /api/* to the backend. A
 * non-2xx answer throws GamesApiError with the body's `detail`, and a 401 also
 * tells the app to show the login.
 */

import { notifyUnauthorized } from '@/api/unauthorized'

/** Raised for any non-2xx API response, carrying the HTTP status. */
export class GamesApiError extends Error {
  readonly status: number
  /** Seconds from a Retry-After header (a 503 for an unpublished lineup). */
  readonly retryAfter: number | null
  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message)
    this.name = 'GamesApiError'
    this.status = status
    this.retryAfter = retryAfter
  }
}

/** The Retry-After header in seconds, or null when absent or not a number. */
function retryAfterOf(res: Response): number | null {
  const v = Number(res.headers.get('Retry-After'))
  return Number.isFinite(v) && v > 0 ? v : null
}

/** Send one request and read its JSON body; a non-2xx answer throws. */
async function requestJson<T>(url: string, init: RequestInit, withRetryAfter = false): Promise<T> {
  const res = await fetch(url, { credentials: 'include', ...init })
  if (!res.ok) {
    if (res.status === 401) notifyUnauthorized()
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(
      res.status,
      body.detail ?? `Request failed (${res.status}).`,
      withRetryAfter ? retryAfterOf(res) : null,
    )
  }
  return res.json() as Promise<T>
}

/** GET a JSON body. */
export function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  return requestJson<T>(url, { signal })
}

export interface PostOptions {
  signal?: AbortSignal
  /** Put the Retry-After header on the error (the game routes' 503). */
  retryAfter?: boolean
}

/** POST a JSON payload and read the JSON body. */
export function postJson<T>(url: string, payload: unknown, opts: PostOptions = {}): Promise<T> {
  return requestJson<T>(
    url,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: opts.signal,
    },
    opts.retryAfter,
  )
}

/** DELETE and read the JSON body. */
export function deleteJson<T>(url: string): Promise<T> {
  return requestJson<T>(url, { method: 'DELETE' })
}
