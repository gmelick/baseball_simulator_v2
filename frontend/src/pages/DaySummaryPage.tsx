/**
 * DaySummaryPage.tsx — the Daily Diamond day slate (SIM-391; rebuilt for SIM-519).
 *
 * The date lives in the URL (`/date/:date`; `/` is today). One call to
 * GET /api/games/{date} lists the day from the league's schedule. The page:
 *
 *   - sorts the cards live first, then scheduled by first pitch, then final,
 *     then postponed;
 *   - polls the slate every 30 seconds while a game is live or the date is
 *     today, and only while the tab is visible;
 *   - opens a live card by default on a wide screen, and no card on a phone;
 *   - says so when the league feed is down and the list is a stored copy.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import {
  cardStatus,
  fetchGamesOnDate,
  GamesApiError,
  type GameCard as GameCardRow,
  type GamesOnDateResponse,
} from '@/api/games'
import { GameCard } from '@/components/games/GameCard'
import { DatePicker } from '@/components/slate/DatePicker'
import { SLATE_POLL_MS, sortSlate } from '@/components/slate/slate'
import { ISO_DATE_RE, longDateLabel, relativeDayLabel, todayIso } from '@/components/slate/format'

import styles from './DaySummaryPage.module.css'

const PHONE_QUERY = '(max-width: 640px)'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ok'; data: GamesOnDateResponse }
  | { kind: 'error'; message: string; status?: number }

function usePhone(): boolean {
  const [phone, setPhone] = useState(() => window.matchMedia?.(PHONE_QUERY).matches ?? false)
  useEffect(() => {
    const mq = window.matchMedia?.(PHONE_QUERY)
    if (!mq) return
    const on = (): void => setPhone(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return phone
}

export function DaySummaryPage(): React.ReactElement {
  const params = useParams<{ date?: string }>()
  const navigate = useNavigate()
  const date = params.date && ISO_DATE_RE.test(params.date) ? params.date : todayIso()
  const phone = usePhone()

  const [state, setState] = useState<LoadState>({ kind: 'loading' })
  const [tick, setTick] = useState(0)
  // A card's open state flips its default (live cards open on a wide screen).
  const [flipped, setFlipped] = useState<Record<number, boolean>>({})

  // A new date or a new screen class resets the cards to their defaults.
  useEffect(() => setFlipped({}), [date, phone])

  // The first load of a date shows the loading state.
  useEffect(() => {
    let cancelled = false
    setState({ kind: 'loading' })
    fetchGamesOnDate(date)
      .then((data) => {
        if (!cancelled) setState({ kind: 'ok', data })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setState({
          kind: 'error',
          message: err instanceof Error ? err.message : 'Failed to load the slate.',
          status: err instanceof GamesApiError ? err.status : undefined,
        })
      })
    return () => {
      cancelled = true
    }
  }, [date])

  // A refresh replaces the slate quietly; a failed refresh keeps it on screen.
  useEffect(() => {
    if (tick === 0) return
    let cancelled = false
    fetchGamesOnDate(date)
      .then((data) => {
        if (!cancelled) setState({ kind: 'ok', data })
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
    // The date's own load runs in the effect above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick])

  const games = useMemo(() => (state.kind === 'ok' ? sortSlate(state.data.games) : []), [state])
  const liveCount = games.filter((g) => cardStatus(g) === 'live').length
  const finalCount = games.filter((g) => cardStatus(g) === 'final').length
  const isToday = date === todayIso()

  // Poll while a game is live or the date is today, and only while visible.
  useEffect(() => {
    if (state.kind !== 'ok' || !(liveCount > 0 || isToday)) return
    const id = window.setInterval(() => {
      if (document.visibilityState === 'visible') setTick((t) => t + 1)
    }, SLATE_POLL_MS)
    const onVisible = (): void => {
      if (document.visibilityState === 'visible') setTick((t) => t + 1)
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [state.kind, liveCount, isToday])

  const goToDate = useCallback((next: string) => navigate(`/date/${next}`), [navigate])

  const isOpen = (g: GameCardRow): boolean => {
    const byDefault = !phone && cardStatus(g) === 'live'
    return flipped[g.game_pk] ? !byDefault : byDefault
  }

  const source = state.kind === 'ok' ? state.data.source : null

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <div className={styles.kicker}>{relativeDayLabel(date)}</div>
          <h1 className={styles.title}>{longDateLabel(date)}</h1>
        </div>
        <div className={styles.headerRight}>
          {state.kind === 'ok' && (
            <div className={styles.tags} aria-live="polite">
              <span className={`${styles.tag} ${styles.tagCount}`}>
                {games.length} {games.length === 1 ? 'game' : 'games'}
              </span>
              {liveCount > 0 && <span className={`${styles.tag} ${styles.tagLive}`}>{liveCount} live</span>}
              {finalCount > 0 && finalCount < games.length && (
                <span className={`${styles.tag} ${styles.tagFinal}`}>{finalCount} final</span>
              )}
            </div>
          )}
          <DatePicker date={date} onChange={goToDate} wide={!phone} />
        </div>
      </header>

      {source && source !== 'schedule' && (
        <div className={styles.banner} role="status">
          {source === 'schedule_cached'
            ? 'The league schedule feed is unavailable. Showing the last good copy of the schedule.'
            : 'The league schedule feed is unavailable. Showing stored games.'}
          {state.kind === 'ok' && state.data.feed_error && (
            <span className={styles.bannerDetail}> ({state.data.feed_error})</span>
          )}
        </div>
      )}

      {state.kind === 'loading' && (
        <p className={styles.message} aria-busy="true">
          Loading the slate…
        </p>
      )}

      {state.kind === 'error' && (
        <div className={styles.error} role="alert">
          <p>{state.message}</p>
          {state.status === 401 && <p className={styles.errorHint}>Your session may have expired — try refreshing.</p>}
        </div>
      )}

      {state.kind === 'ok' && games.length === 0 && (
        <div className={styles.empty}>No MLB games on {longDateLabel(date)}.</div>
      )}

      {games.length > 0 && (
        <div className={styles.grid}>
          {games.map((game) => (
            <GameCard
              key={game.game_pk}
              game={game}
              open={isOpen(game)}
              tick={tick}
              onToggle={() => setFlipped((f) => ({ ...f, [game.game_pk]: !f[game.game_pk] }))}
            />
          ))}
        </div>
      )}
    </div>
  )
}
