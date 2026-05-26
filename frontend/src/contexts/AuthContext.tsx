/**
 * AuthContext.tsx — SIM-389
 *
 * Provides auth state (authenticated, loading) and actions (login, logout)
 * to the entire component tree.
 *
 * On mount, calls GET /auth/me to restore a pre-existing session — so a
 * page refresh does NOT log the user out (the httpOnly cookie persists).
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

// ---------------------------------------------------------------------------
// Shape
// ---------------------------------------------------------------------------

interface AuthContextValue {
  /** True once the /auth/me probe has completed. */
  loading: boolean
  /** True when a valid session or dev bypass is active. */
  authenticated: boolean
  /** Auth mode reported by the server ("session" | "dev_bypass" | …). */
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
  const [mode, setMode] = useState('unauthenticated')

  // Probe the session on mount (restores existing cookie across page reloads).
  useEffect(() => {
    checkAuth()
      .then((status) => {
        setAuthenticated(status.authenticated)
        setMode(status.mode)
      })
      .catch(() => {
        setAuthenticated(false)
        setMode('unauthenticated')
      })
      .finally(() => setLoading(false))
  }, [])

  const login = useCallback(async (password: string) => {
    await apiLogin(password)
    // Re-probe so we get the canonical mode from the server.
    const status = await checkAuth()
    setAuthenticated(status.authenticated)
    setMode(status.mode)
  }, [])

  const logout = useCallback(async () => {
    await apiLogout()
    setAuthenticated(false)
    setMode('unauthenticated')
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

// A context file conventionally co-locates its provider component and its
// consumer hook; fast-refresh of AuthProvider is not a concern (mounted once
// at the root), so the react-refresh rule is disabled for this export.
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be called inside <AuthProvider>.')
  }
  return ctx
}
