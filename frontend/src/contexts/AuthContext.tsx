/**
 * AuthContext.tsx — the auth state (authenticated, loading) and the auth
 * actions (login, logout) for the whole component tree.
 *
 * On mount, the provider calls GET /auth/me to restore an existing session.
 * A page refresh therefore does NOT log the user out (the httpOnly cookie
 * persists).
 *
 * A data client that gets a 401 fires UNAUTHORIZED_EVENT
 * (@/api/unauthorized). The provider listens and marks the session logged
 * out, so App renders the login page after the session expires.
 *
 * Usage:
 *   const { authenticated, loading, login, logout } = useAuth()
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

import {
  checkAuth,
  login as apiLogin,
  logout as apiLogout,
} from '@/api/auth'
import { UNAUTHORIZED_EVENT } from '@/api/unauthorized'

// ---------------------------------------------------------------------------
// Shape
// ---------------------------------------------------------------------------

interface AuthContextValue {
  /** True while the /auth/me probe runs; false once it completes. */
  loading: boolean
  /** True when the server accepts this browser: a valid session cookie, or
   *  a server that runs with AUTH_DISABLED=1. */
  authenticated: boolean
  /** Auth mode reported by the server ("cookie" | "api_key" | "disabled" | "none"). */
  mode: string
  /** Call with the shared password to log in. Throws on wrong password. */
  login: (password: string) => Promise<void>
  /** Log out and clear the session cookie. */
  logout: () => Promise<void>
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const AuthContext = createContext<AuthContextValue | null>(null)

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true)
  const [authenticated, setAuthenticated] = useState(false)
  const [mode, setMode] = useState('none')

  // Probe the session on mount. The probe restores an existing cookie session after a page reload.
  useEffect(() => {
    checkAuth()
      .then((status) => {
        setAuthenticated(status.authenticated)
        setMode(status.mode)
      })
      .catch(() => {
        setAuthenticated(false)
        setMode('none')
      })
      .finally(() => setLoading(false))
  }, [])

  // A 401 from any data client means the session ended: show the login page.
  useEffect(() => {
    const onUnauthorized = () => {
      setAuthenticated(false)
      setMode('none')
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  const login = useCallback(async (password: string) => {
    await apiLogin(password)
    // Probe again to read the auth mode from the server.
    const status = await checkAuth()
    setAuthenticated(status.authenticated)
    setMode(status.mode)
  }, [])

  const logout = useCallback(async () => {
    await apiLogout()
    setAuthenticated(false)
    setMode('none')
  }, [])

  const value = useMemo(
    () => ({ loading, authenticated, mode, login, logout }),
    [loading, authenticated, mode, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

// A context file keeps its provider and its consumer hook together.
// AuthProvider mounts once at the root, so fast refresh does not matter here.
// The react-refresh rule is therefore off for this export.
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be called inside <AuthProvider>.')
  }
  return ctx
}
