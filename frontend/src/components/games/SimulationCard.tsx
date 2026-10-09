/**
 * SimulationCard.tsx — one simulation run per game (SIM-519 Part E).
 *
 * A size menu (100 · 300 · 1,000 games, with a time estimate), Run, and the
 * run's state: queued shows its place in line, running a progress bar
 * (polled every 2 seconds), done shows the size, the age and the lineup the
 * run used, with Re-run; failed shows the error. On mount the card reads the
 * game's newest run, so a reload resumes the bar. Cancel stops a run between
 * chunks of games. A game whose lineup is not published yet shows the server's
 * words and a countdown to the next try.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react'

import {
  cancelSimRun,
  fetchLatestRun,
  fetchSimRun,
  GamesApiError,
  startSimRun,
  type SimRun,
} from '@/api/games'
import { ageLabel } from '@/components/slate/format'

import styles from './SimulationCard.module.css'

/** The run sizes on offer (decision D5). */
const RUN_SIZES = [100, 300, 1000] as const
/** About 0.3 s a game on the warm pool (the n=100 run reads 25–35 s). */
const SECONDS_PER_GAME = 0.3
const POLL_MS = 2000

const LINEUP_LABEL: Record<string, string> = {
  box: 'final lineup',
  published: 'published lineup',
  projected: 'projected lineup (last game)',
}

function estimate(n: number): string {
  const s = Math.round(n * SECONDS_PER_GAME)
  return s < 90 ? `~${s} s` : `~${Math.round(s / 60)} min`
}

export interface SimulationCardProps {
  gamePk: number
  /** Called once with each newly finished run (and the newest done run on mount). */
  onDone?: (run: SimRun) => void
}

export function SimulationCard({ gamePk, onDone }: SimulationCardProps): React.ReactElement {
  const [size, setSize] = useState<number>(RUN_SIZES[0])
  const [run, setRun] = useState<SimRun | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retryIn, setRetryIn] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const reported = useRef<number | null>(null)

  const report = useCallback(
    (r: SimRun) => {
      if (r.status === 'done' && reported.current !== r.run_id) {
        reported.current = r.run_id
        onDone?.(r)
      }
    },
    [onDone],
  )

  // The newest run on mount: resume a running bar, or show the last result.
  useEffect(() => {
    let cancelled = false
    fetchLatestRun(gamePk)
      .then((r) => {
        if (cancelled) return
        setRun(r)
        setSize(RUN_SIZES.includes(r.n_iterations as (typeof RUN_SIZES)[number]) ? r.n_iterations : RUN_SIZES[0])
        report(r)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [gamePk, report])

  // Poll while queued or running.
  const active = run != null && (run.status === 'queued' || run.status === 'running')
  useEffect(() => {
    if (!active || run == null) return
    const id = window.setInterval(() => {
      fetchSimRun(gamePk, run.run_id)
        .then((r) => {
          setRun(r)
          report(r)
        })
        .catch(() => undefined)
    }, POLL_MS)
    return () => window.clearInterval(id)
  }, [active, gamePk, run, report])

  // The Retry-After countdown of an unpublished lineup.
  useEffect(() => {
    if (retryIn == null || retryIn <= 0) return
    const id = window.setTimeout(() => setRetryIn((s) => (s == null ? null : s - 1)), 1000)
    return () => window.clearTimeout(id)
  }, [retryIn])

  const start = (): void => {
    setBusy(true)
    setError(null)
    setRetryIn(null)
    startSimRun(gamePk, size)
      .then((r) => {
        setRun(r)
        report(r)
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : 'The run could not start.')
        if (err instanceof GamesApiError && err.status === 503 && err.retryAfter) setRetryIn(err.retryAfter)
      })
      .finally(() => setBusy(false))
  }

  const cancel = (): void => {
    if (!run) return
    cancelSimRun(gamePk, run.run_id)
      .then((r) => setRun(r))
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Cancel failed.'))
  }

  const pct = run && run.n_iterations > 0 ? Math.min(100, Math.round((run.progress_done / run.n_iterations) * 100)) : 0
  const waiting = retryIn != null && retryIn > 0

  return (
    <section className={styles.card} aria-label="Simulation">
      <div className={styles.row}>
        <h2 className={styles.title}>Simulation</h2>
        <div className={styles.sizes} role="radiogroup" aria-label="Run size">
          {RUN_SIZES.map((n) => (
            <button
              key={n}
              type="button"
              role="radio"
              aria-checked={size === n}
              className={`${styles.size} ${size === n ? styles.sizeOn : ''}`}
              onClick={() => setSize(n)}
              disabled={active}
            >
              {n.toLocaleString()} <span className={styles.est}>{estimate(n)}</span>
            </button>
          ))}
        </div>
        {active ? (
          <button type="button" className={styles.secondary} onClick={cancel}>
            Cancel
          </button>
        ) : (
          <button type="button" className={styles.primary} onClick={start} disabled={busy || waiting}>
            {run?.status === 'done' ? 'Re-run' : 'Run'}
          </button>
        )}
      </div>

      {run?.status === 'queued' && (
        <p className={styles.state} role="status">
          Queued{run.position_in_queue ? ` · #${run.position_in_queue} in line` : ''}…
        </p>
      )}
      {run?.status === 'running' && (
        <div className={styles.state} role="status">
          <div
            className={styles.bar}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={run.n_iterations}
            aria-valuenow={run.progress_done}
            aria-label="Simulation progress"
          >
            <div className={styles.fill} style={{ width: `${pct}%` }} />
          </div>
          <span className={styles.mono}>
            {run.progress_done} / {run.n_iterations} games
          </span>
        </div>
      )}
      {run?.status === 'done' && (
        <p className={styles.state}>
          {run.n_iterations.toLocaleString()} games · {ageLabel(run.finished_at) ?? 'just now'}
          {run.lineup_source ? ` · ${LINEUP_LABEL[run.lineup_source] ?? run.lineup_source}` : ''}
          {run.bullpen_source === 'synthetic' ? ' · generic bullpen' : ''}
        </p>
      )}
      {run?.status === 'cancelled' && (
        <p className={styles.state}>
          Cancelled after {run.progress_done} of {run.n_iterations} games.
        </p>
      )}
      {run?.status === 'failed' && (
        <p className={styles.error} role="alert">
          The run failed{run.error ? `: ${run.error}` : '.'}
        </p>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}
          {waiting ? ` Try again in ${retryIn} s.` : ''}
        </p>
      )}
    </section>
  )
}
