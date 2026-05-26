/**
 * DaySummaryPage.tsx — SIM-391
 *
 * The slate view: a date navigator (prev / next / today + native date picker),
 * a game-count badge, and a responsive grid of 3-state GameCards for the
 * selected date. The date lives in the URL (`/date/:date`) so a slate is
 * shareable and the browser back/forward buttons move between days; `/` falls
 * back to today.
 *
 * Data: GET /api/games/{date} (one call for the whole slate). Loading, empty,
 * and error states are all handled inline.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import {
  fetchGamesOnDate,
  GamesApiError,
  type GameCard as GameCardRow,
} from '@/api/games'
import { GameCard } from '@/components/games/GameCard'
import { Badge } from '@/components/ui'

import styles from './DaySummaryPage.module.css'

// ---------------------------------------------------------------------------
// Date helpers — operate on local YYYY-MM-DD strings (no UTC drift).
// ---------------------------------------------------------------------------

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/

function todayIso(): string {
  const d = new Date()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

/** Shift a YYYY-MM-DD string by `delta` days, returning a YYYY-MM-DD string. */
function shiftIso(iso: string, delta: number): string {
  const [y, m, d] = iso.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  dt.setDate(dt.getDate() + delta)
  const mm = String(dt.getMonth() + 1).padStart(2, '0')
  const dd = String(dt.getDate()).padStart(2, '0')
  return `${dt.getFullYear()}-${mm}-${dd}`
}

/** Human-friendly long date for the header (e.g. "Mon, Aug 15, 2024"). */
function prettyDate(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  return dt.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

// ---------------------------------------------------------------------------
// Fetch state machine
// ---------------------------------------------------------------------------

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ok'; games: GameCardRow[] }
  | { kind: 'error'; message: string; status?: number }

export function DaySummaryPage(): React.ReactElement {
  const params = useParams<{ date?: string }>()
  const navigate = useNavigate()

  // Validate the URL date; fall back to today for a missing/garbage value.
  const date = params.date && ISO_DATE_RE.test(params.date) ? params.date : todayIso()

  const [state, setState] = useState<LoadState>({ kind: 'loading' })

  useEffect(() => {
    let cancelled = false
    setState({ kind: 'loading' })
    fetchGamesOnDate(date)
      .then((res) => {
        if (!cancelled) setState({ kind: 'ok', games: res.games })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const status = err instanceof GamesApiError ? err.status : undefined
        const message =
          err instanceof Error ? err.message : 'Failed to load the slate.'
        setState({ kind: 'error', message, status })
      })
    return () => {
      cancelled = true
    }
  }, [date])

  const goToDate = useCallback(
    (next: string) => navigate(`/date/${next}`),
    [navigate],
  )

  const count = state.kind === 'ok' ? state.games.length : undefined

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>Day Summary</h1>
          {count != null && (
            <Badge variant="primary" aria-label={`${count} games`}>
              {count} {count === 1 ? 'game' : 'games'}
            </Badge>
          )}
        </div>

        <nav className={styles.dateNav} aria-label="Date navigation">
          <button
            type="button"
            className={styles.navButton}
            onClick={() => goToDate(shiftIso(date, -1))}
            aria-label="Previous day"
          >
            ‹
          </button>

          <input
            type="date"
            className={styles.datePicker}
            value={date}
            onChange={(e) => {
              if (ISO_DATE_RE.test(e.target.value)) goToDate(e.target.value)
            }}
            aria-label="Select date"
          />

          <button
            type="button"
            className={styles.navButton}
            onClick={() => goToDate(shiftIso(date, 1))}
            aria-label="Next day"
          >
            ›
          </button>

          <button
            type="button"
            className={styles.todayButton}
            onClick={() => goToDate(todayIso())}
          >
            Today
          </button>

          <span className={styles.prettyDate}>{prettyDate(date)}</span>
        </nav>
      </header>

      <main>
        {state.kind === 'loading' && (
          <p className={styles.message} aria-busy="true">
            Loading slate…
          </p>
        )}

        {state.kind === 'error' && (
          <div className={styles.error} role="alert">
            <p>{state.message}</p>
            {state.status === 401 && (
              <p className={styles.errorHint}>Your session may have expired — try refreshing.</p>
            )}
          </div>
        )}

        {state.kind === 'ok' && state.games.length === 0 && (
          <p className={styles.message}>No games scheduled on {prettyDate(date)}.</p>
        )}

        {state.kind === 'ok' && state.games.length > 0 && (
          <div className={styles.grid}>
            {state.games.map((game) => (
              <GameCard key={game.game_pk} game={game} />
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
