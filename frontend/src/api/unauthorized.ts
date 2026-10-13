/**
 * unauthorized.ts — SIM-443c
 * The one signal that a data request came back 401 (no valid credential).
 *
 * Every API route now requires a login. When the session cookie expires,
 * each data client calls notifyUnauthorized() on a 401. AuthContext listens
 * for the event and marks the session logged out, so App renders the login
 * page instead of a wall of error cards.
 *
 * The auth client (auth.ts) does NOT call it: a 401 from /auth/login is a
 * wrong password, not an expired session.
 */

/** The window event a data client fires on a 401 response. */
export const UNAUTHORIZED_EVENT = 'sim:unauthorized'

/** Tell the auth context that the server refused the session. */
export function notifyUnauthorized(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
  }
}
