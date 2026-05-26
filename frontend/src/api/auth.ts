/**
 * auth.ts — SIM-389
 * Browser-side auth API client.
 *
 * All requests use `credentials: 'include'` so the httpOnly `sim_session`
 * cookie is sent automatically on every call (set by POST /auth/login).
 * The Vite dev-server proxy forwards /auth/* to localhost:8000 and passes
 * the Set-Cookie response back to the browser unchanged.
 */

export interface AuthStatus {
  authenticated: boolean
  /** "session" | "api_key" | "dev_bypass" | "unauthenticated" */
  mode: string
  /** Seconds until the current session expires (0 when not authenticated). */
  expires_in: number
}

/** POST /auth/login — exchange password for session cookie.
 *  Throws on a wrong password (401) or network error. */
export async function login(password: string): Promise<void> {
  const res = await fetch('/auth/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error((body as { detail?: string }).detail ?? 'Login failed.')
  }
}

/** GET /auth/me — probe the current session without triggering a 401.
 *  Always resolves; returns {authenticated: false} when no session. */
export async function checkAuth(): Promise<AuthStatus> {
  const res = await fetch('/auth/me', { credentials: 'include' })
  if (!res.ok) {
    return { authenticated: false, mode: 'unauthenticated', expires_in: 0 }
  }
  return res.json() as Promise<AuthStatus>
}

/** POST /auth/logout — expire the session cookie. */
export async function logout(): Promise<void> {
  await fetch('/auth/logout', { method: 'POST', credentials: 'include' })
}
