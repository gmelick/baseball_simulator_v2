/**
 * states.tsx — SIM-439
 * Shared load / empty / error / engine-warming / not-configured states for the
 * Data Lab + Similarity Explorer. Keeps every page's failure modes consistent
 * with the existing app (role=alert on errors, aria-busy on loading).
 */
import React from 'react'

import { Badge } from '@/components/ui'

import styles from './states.module.css'

export function LoadingBlock({ label = 'Loading…' }: { label?: string }): React.ReactElement {
  return (
    <div className={styles.loading} aria-busy="true" aria-label={label}>
      <span className={styles.spinner} aria-hidden="true" />
      {label}
    </div>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: React.ReactNode }): React.ReactElement {
  return (
    <div className={styles.empty}>
      <p className={styles.emptyTitle}>{title}</p>
      {hint && <p className={styles.emptyHint}>{hint}</p>}
    </div>
  )
}

export function ErrorBlock({ message, hint }: { message: string; hint?: React.ReactNode }): React.ReactElement {
  return (
    <div className={styles.error} role="alert">
      <p>{message}</p>
      {hint && <p className={styles.errorHint}>{hint}</p>}
    </div>
  )
}

export function EngineWarmingState({
  engine,
  onRetry,
}: {
  engine: string
  onRetry?: () => void
}): React.ReactElement {
  return (
    <div className={styles.warming}>
      <Badge variant="info">warming</Badge>
      <p className={styles.warmingTitle}>The {engine} engine is still warming up</p>
      <p className={styles.emptyHint}>
        Similarity engines build their profiles from DuckDB at boot. This can take a moment after a
        restart, and some engines may not be built in every deployment.
      </p>
      {onRetry && (
        <button type="button" className={styles.retryBtn} onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}

export function NotConfiguredState(): React.ReactElement {
  return (
    <div className={styles.notConfigured}>
      <span className={styles.notConfiguredIcon} aria-hidden="true">
        🤖
      </span>
      <p className={styles.emptyTitle}>The AI assistant is not configured</p>
      <p className={styles.emptyHint}>
        The assistant is an optional integration. To enable it, set an{' '}
        <code className={styles.code}>ANTHROPIC_API_KEY</code> in the server environment and restart
        the API. It writes read-only SQL, runs it through the same safe path as the SQL Console, and
        explains the results.
      </p>
      <p className={styles.emptyHint}>
        In the meantime, the <strong>SQL Console</strong> gives you the full query surface.
      </p>
    </div>
  )
}
