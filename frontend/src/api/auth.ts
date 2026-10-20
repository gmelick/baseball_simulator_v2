/**
 * auth.ts — the browser's auth API client.
 *
 * Every request uses `credentials: 'include'`, so the browser sends the
 * httpOnly `sim_session` cookie on each call. POST /auth/login sets the
 * cookie. In development, the Vite dev-server proxy forwards /auth/* to the
 * backend (localhost:8000 by default). The proxy passes the Set-Cookie
 * response back to the browser unchanged.
 */

export interface AuthStatus {
  authenticated: boolean
  /** "cookie" | "api_key" | "disabled" | "none". "disabled" means the server
   *  runs with AUTH_DISABLED=1 and checks no credential. */
  mode: string
  /** Seconds until the current session expires (0 when not authenticated). */
  expires_in: number
}

/** POST /auth/login — exchange the password for a session cookie.
 *  Throws when the server refuses the login (a wrong password is a 401)
 *  or the network fails. */
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
 *  Returns {authenticated: false} on a non-OK response; a network error
 *  rejects. */
export async function checkAuth(): Promise<AuthStatus> {
  const res = await fetch('/auth/me', { credentials: 'include' })
  if (!res.ok) {
    return { authenticated: false, mode: 'none', expires_in: 0 }
  }
  return res.json() as Promise<AuthStatus>
}

/** POST /auth/logout — expire the session cookie. */
export async function logout(): Promise<void> {
  await fetch('/auth/logout', { method: 'POST', credentials: 'include' })
}
