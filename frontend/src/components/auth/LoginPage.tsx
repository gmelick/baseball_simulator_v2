/**
 * LoginPage.tsx — SIM-389
 *
 * Full-page login screen shown when no valid session exists.
 * Calls AuthContext.login() on submit; shows an inline error on 401.
 *
 * Layout: centred card with the ⚾ logo, a password field, and a submit button.
 * Keyboard: Enter submits; Escape clears the error.
 */

import React, { useCallback, useRef, useState } from 'react'

import { useAuth } from '@/contexts/AuthContext'
import styles from './LoginPage.module.css'

export function LoginPage() {
  const { login } = useAuth()
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      if (!password.trim() || submitting) return

      setSubmitting(true)
      setError(null)

      try {
        await login(password)
        // AuthContext sets authenticated=true → App unmounts this page.
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Login failed.'
        setError(msg)
        setPassword('')
        // Return focus to the input so the user can retype immediately.
        setTimeout(() => inputRef.current?.focus(), 0)
      } finally {
        setSubmitting(false)
      }
    },
    [login, password, submitting],
  )

  return (
    <div className={styles.backdrop}>
      <div className={styles.card} role="main" aria-label="Log in to MLB Sim Platform">
        <div className={styles.logo} aria-hidden="true">⚾</div>
        <h1 className={styles.title}>MLB Sim Platform</h1>
        <p className={styles.subtitle}>Enter your access password to continue.</p>

        <form onSubmit={handleSubmit} className={styles.form} noValidate>
          <label htmlFor="sim-password" className={styles.label}>
            Password
          </label>
          <input
            id="sim-password"
            ref={inputRef}
            type="password"
            className={styles.input}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Escape' && setError(null)}
            placeholder="Enter password"
            autoComplete="current-password"
            autoFocus
            disabled={submitting}
            aria-describedby={error ? 'login-error' : undefined}
            aria-invalid={!!error}
          />

          {error && (
            <p id="login-error" className={styles.error} role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            className={styles.button}
            disabled={submitting || !password.trim()}
            aria-busy={submitting}
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
