/**
 * GamePage.tsx — SIM-393
 *
 * The game detail view at /game/:gamePk. Composes:
 *   - a header (matchup, 3-state status, score)
 *   - a live banner (WS connection + re-simulating indicator) when in progress
 *   - the linescore graphic (SIM-392) + the field graphic (SIM-392)
 *   - the play-by-play scroll (SIM-393)
 *   - live updates over the per-game WebSocket (SIM-385 schema)
 *
 * Data: /status (identity+status+score), /live (in-progress field state),
 * /linescore + /plays (need a persisted sim run — a 404 is treated as
 * "no data yet", not an error). Per-player projections (SIM-394) and the
 * betting card (SIM-395) mount into the marked slots in later tickets.
 */
import React, { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  fetchGameCard,
  fetchLinescore,
  fetchLiveState,
  fetchPlays,
  GamesApiError,
  type GameCardAggregate,
  type Linescore,
  type LiveState,
  type PlayByPlay,
} from '@/api/games'
import { BaseballFieldGraphic, LinescoreGraphic } from '@/components/graphics'
import { PlayByPlayList } from '@/components/games/PlayByPlayList'
import { Badge, Card, Panel } from '@/components/ui'
import { useGameSocket } from '@/hooks/useGameSocket'

import styles from './GamePage.module.css'

function teamLabel(name: string | null, abbrev: string | null, id: number | null): string {
  return name ?? abbrev ?? (id != null ? `Team ${id}` : 'TBD')
}

/** Fetch hook that tolerates a 404 as "no data yet" (returns null, not error). */
function useOptionalResource<T>(
  fn: () => Promise<T>,
  deps: React.DependencyList,
): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    fn()
      .then((d) => !cancelled && setData(d))
      .catch((err: unknown) => {
        if (cancelled) return
        // A 404 means the resource doesn't exist yet (no sim / not live) — not an error.
        if (err instanceof GamesApiError && err.status === 404) {
          setData(null)
          return
        }
        setError(err instanceof Error ? err.message : 'Failed to load.')
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return { data, error }
}

function StatusBadge({ status }: { status: string }): React.ReactElement {
  switch (status) {
    case 'live':
      return <Badge variant="live" pulse>LIVE</Badge>
    case 'final':
      return <Badge variant="final">FINAL</Badge>
    case 'postponed':
      return <Badge variant="default">POSTPONED</Badge>
    default:
      return <Badge variant="scheduled">SCHEDULED</Badge>
  }
}

export function GamePage(): React.ReactElement {
  const { gamePk: gamePkParam } = useParams<{ gamePk: string }>()
  const gamePk = Number(gamePkParam)
  const valid = Number.isFinite(gamePk) && gamePk > 0

  const card = useOptionalResource<GameCardAggregate>(
    () => fetchGameCard(gamePk),
    [gamePk],
  )
  const isLive = card.data?.game_status === 'live'

  const live = useOptionalResource<LiveState>(
    () => (isLive ? fetchLiveState(gamePk) : Promise.resolve(null as unknown as LiveState)),
    [gamePk, isLive],
  )
  const linescore = useOptionalResource<Linescore>(() => fetchLinescore(gamePk), [gamePk])
  const plays = useOptionalResource<PlayByPlay>(() => fetchPlays(gamePk), [gamePk])

  // Live WS — only connect for an in-progress game.
  const socket = useGameSocket(gamePk, isLive)

  if (!valid) {
    return (
      <div className={styles.page}>
        <p>Invalid game id.</p>
        <Link to="/">‹ Back to slate</Link>
      </div>
    )
  }

  const c = card.data
  const away = c ? teamLabel(c.away_team_name, c.away_team_abbrev, c.away_team_id) : 'Away'
  const home = c ? teamLabel(c.home_team_name, c.home_team_abbrev, c.home_team_id) : 'Home'

  // Score precedence: live WS > REST live > final scores from the card.
  const wsState = socket.liveState
  const awayScore =
    wsState?.away_score ?? live.data?.away_score ?? c?.away_score_final ?? null
  const homeScore =
    wsState?.home_score ?? live.data?.home_score ?? c?.home_score_final ?? null

  // Baserunner state for the field graphic (WS first, then REST live).
  const on1 = wsState?.on_1b ?? live.data?.on_1b ?? null
  const on2 = wsState?.on_2b ?? live.data?.on_2b ?? null
  const on3 = wsState?.on_3b ?? live.data?.on_3b ?? null
  const inning = wsState?.inning ?? live.data?.inning ?? null
  const half = wsState?.half ?? live.data?.half ?? null
  const outs = wsState?.outs ?? live.data?.outs ?? null

  const ls = linescore.data

  return (
    <div className={styles.page}>
      <Link to="/" className={styles.back}>
        ‹ Back to slate
      </Link>

      {/* Header */}
      <header className={styles.header}>
        <div className={styles.matchup}>
          <span className={styles.team}>{away}</span>
          {awayScore != null && <span className={styles.score}>{awayScore}</span>}
          <span className={styles.at}>@</span>
          {homeScore != null && <span className={styles.score}>{homeScore}</span>}
          <span className={styles.team}>{home}</span>
        </div>
        <div className={styles.statusLine}>
          {c && <StatusBadge status={c.game_status} />}
          {isLive && inning != null && (
            <span className={styles.inning}>
              {half ?? ''} {inning}
              {outs != null ? ` · ${outs} out` : ''}
            </span>
          )}
        </div>
      </header>

      {/* Live banner */}
      {isLive && (
        <div className={styles.liveBanner} role="status">
          <span className={styles.wsDot} data-status={socket.status} aria-hidden="true" />
          <span>
            {socket.status === 'open'
              ? 'Live updates connected'
              : socket.status === 'connecting'
                ? 'Connecting to live updates…'
                : 'Live updates disconnected'}
          </span>
          {socket.resimPending && <Badge variant="warning">Re-simulating…</Badge>}
        </div>
      )}

      {card.error && (
        <div className={styles.error} role="alert">
          {card.error}
        </div>
      )}

      {/* Main grid */}
      <div className={styles.grid}>
        <div className={styles.leftCol}>
          <Card title="Linescore">
            {ls ? (
              <LinescoreGraphic
                away={{
                  name: c?.away_team_abbrev ?? away,
                  byInning: ls.away_by_inning,
                  runs: ls.away_runs,
                  hits: ls.away_hits,
                  errors: ls.away_errors,
                }}
                home={{
                  name: c?.home_team_abbrev ?? home,
                  byInning: ls.home_by_inning,
                  runs: ls.home_runs,
                  hits: ls.home_hits,
                  errors: ls.home_errors,
                }}
              />
            ) : (
              <p className={styles.muted}>No linescore yet — run a simulation to populate it.</p>
            )}
          </Card>

          {isLive && (
            <Card title="On the field">
              <BaseballFieldGraphic
                onFirst={on1 != null}
                onSecond={on2 != null}
                onThird={on3 != null}
              />
            </Card>
          )}

          <Panel label="Projections (SIM-394)">
            <p className={styles.muted}>Per-player projections + distributions mount here (SIM-394).</p>
          </Panel>

          <Panel label="Betting (SIM-395)">
            <p className={styles.muted}>The betting card surface mounts here (SIM-395).</p>
          </Panel>
        </div>

        <div className={styles.rightCol}>
          <Card title="Play-by-play" count={plays.data?.n_plate_appearances}>
            <PlayByPlayList entries={plays.data?.entries ?? []} />
          </Card>
        </div>
      </div>
    </div>
  )
}
