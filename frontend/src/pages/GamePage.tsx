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
 *
 * The game page (2026-10-09): a live or final game opens with the REAL game (the
 * league feed's linescore and box score, and its play-by-play); "What if from
 * here" on a play opens the what-if panel at that plate appearance (a preview game
 * opens it at first pitch). The simulation sections follow; Projections and
 * Betting are collapsible and start collapsed.
 *
 * SIM-561: the linescore and the play-by-play show ONE simulated game. The
 * "Simulate a game" button stores a new one (POST /sample-game), and loading
 * projections stores the projections' first game; either one reloads both. The
 * page reads the stored game's card (linescore, run id, seed) first and then
 * that run's plays, so the two panels always show the same game.
 */
import React, { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  fetchGameCard,
  fetchGameFeed,
  fetchLiveState,
  fetchRealPlays,
  fetchPlays,
  fetchReplayCard,
  GamesApiError,
  postSampleGame,
  type GameCardAggregate,
  type GameFeedCard,
  type LiveState,
  type RealPlays,
  type PlayByPlay,
  type ReplayCard,
} from '@/api/games'
import { LinescoreGraphic } from '@/components/graphics'
import { ActualPlayByPlay } from '@/components/games/ActualPlayByPlay'
import { BettingCard } from '@/components/games/BettingCard'
import { BoxscorePanel } from '@/components/games/BoxscorePanel'
import { SimulationCard } from '@/components/games/SimulationCard'
import { WhatIfPanel } from '@/components/games/WhatIfPanel'
import { BoxScore, Linescore, OnTheField } from '@/components/slate/CardDetail'
import { TeamBlock } from '@/components/slate/TeamBlock'
import { localStartTime, shortName } from '@/components/slate/format'
import { PlayByPlayList } from '@/components/games/PlayByPlayList'
import { Badge, Card, Panel } from '@/components/ui'
import { useGameSocket } from '@/hooks/useGameSocket'

import styles from './GamePage.module.css'

function teamLabel(name: string | null, abbrev: string | null, id: number | null): string {
  return name ?? abbrev ?? (id != null ? `Team ${id}` : 'TBD')
}

/** SIM-519 Part C: the REST fallback's interval while the live socket is closed. */
const LIVE_POLL_MS = 15_000

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

  // SIM-519 Part C: while the live socket is down, re-read /live every 15 s.
  const [livePoll, setLivePoll] = useState(0)
  const live = useOptionalResource<LiveState>(
    () => (isLive ? fetchLiveState(gamePk) : Promise.resolve(null as unknown as LiveState)),
    [gamePk, isLive, livePoll],
  )
  // The real game: the league feed (linescore, box score, live field) and its
  // plays, for a live or final game; re-read on the live poll.
  const isFinal = card.data?.game_status === 'final'
  const realFeed = useOptionalResource<GameFeedCard | null>(
    () => (isLive || isFinal ? fetchGameFeed(gamePk) : Promise.resolve(null)),
    [gamePk, isLive, isFinal, livePoll],
  )
  const realPlays = useOptionalResource<RealPlays | null>(
    () => (isLive || isFinal ? fetchRealPlays(gamePk) : Promise.resolve(null)),
    [gamePk, isLive, isFinal, livePoll],
  )
  // The what-if's plate appearance: undefined = closed, null = first pitch.
  const [whatIfAt, setWhatIfAt] = useState<number | null | undefined>(undefined)

  // SIM-561: a newly stored simulated game bumps the version, which reloads the
  // card; the card's run id then loads that run's plays.
  const [replayVersion, setReplayVersion] = useState(0)
  // SIM-519 Part E: the game's newest done run; the projections read it.
  const [runId, setRunId] = useState<number | null>(null)
  const [simulating, setSimulating] = useState(false)
  const [simError, setSimError] = useState<string | null>(null)
  const replay = useOptionalResource<ReplayCard>(
    () => fetchReplayCard(gamePk),
    [gamePk, replayVersion],
  )
  const replayRun = replay.data?.run_id ?? null
  const plays = useOptionalResource<PlayByPlay>(
    () =>
      replayRun != null
        ? fetchPlays(gamePk, replayRun)
        : Promise.reject(new GamesApiError(404, 'no stored game')),
    [gamePk, replayRun],
  )

  const showStoredGame = (): void => {
    setReplayVersion((v) => v + 1)
  }

  const simulateOneGame = (): void => {
    setSimulating(true)
    setSimError(null)
    postSampleGame(gamePk)
      .then(() => showStoredGame())
      .catch((err: unknown) =>
        setSimError(err instanceof Error ? err.message : 'The simulation failed.'),
      )
      .finally(() => setSimulating(false))
  }

  // Live WS — only connect for an in-progress game.
  const socket = useGameSocket(gamePk, isLive)

  useEffect(() => {
    if (!isLive) return
    // The socket carries the score; the real box score and plays are re-read
    // every 15 s while live (and /live too, for when the socket is closed).
    const id = window.setInterval(() => setLivePoll((n) => n + 1), LIVE_POLL_MS)
    return () => window.clearInterval(id)
  }, [isLive])

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
    wsState?.away_score ?? live.data?.away_score ?? c?.away_score ?? c?.away_score_final ?? null
  const homeScore =
    wsState?.home_score ?? live.data?.home_score ?? c?.home_score ?? c?.home_score_final ?? null

  const inning = wsState?.inning ?? live.data?.inning ?? null
  const half = wsState?.half ?? live.data?.half ?? null
  const outs = wsState?.outs ?? live.data?.outs ?? null

  const ls = replay.data?.linescore ?? null
  const replaySeed = replay.data?.base_seed ?? null
  const awayShort = c?.away_team_abbrev ?? away
  const homeShort = c?.home_team_abbrev ?? home
  const simButton = (
    <button
      type="button"
      className={styles.simButton}
      onClick={simulateOneGame}
      disabled={simulating || !valid}
    >
      {simulating ? 'Simulating…' : ls ? 'Simulate another' : 'Simulate a game'}
    </button>
  )

  return (
    <div className={styles.page}>
      <Link to="/" className={styles.back}>
        ‹ Back to slate
      </Link>

      {/* Header */}
      {/* SIM-519: the slate card's team block, so the two pages read as one. */}
      <header className={styles.header}>
        <div className={styles.matchupDD}>
          <TeamBlock
            side="away"
            teamId={c?.away_team_id ?? null}
            abbr={c?.away_team_abbrev ?? null}
            name={c?.away_team_name ?? away}
            wins={c?.away_wins ?? null}
            losses={c?.away_losses ?? null}
            dim={c?.game_status === 'final' && awayScore != null && homeScore != null && awayScore < homeScore}
          />
          <div className={styles.center}>
            <div className={styles.bigScore}>
              {awayScore != null && homeScore != null ? `${awayScore} – ${homeScore}` : 'vs'}
            </div>
            <div className={styles.centerSub}>
              {isLive && inning != null
                ? `${half ?? ''} ${inning}${outs != null ? ` · ${outs} out` : ''}`
                : c?.game_status === 'scheduled'
                  ? c.start_time_tbd
                    ? 'Time TBD'
                    : localStartTime(c.start_utc) ?? 'Pregame'
                  : (c?.detailed_state ?? '')}
            </div>
          </div>
          <TeamBlock
            side="home"
            teamId={c?.home_team_id ?? null}
            abbr={c?.home_team_abbrev ?? null}
            name={c?.home_team_name ?? home}
            wins={c?.home_wins ?? null}
            losses={c?.home_losses ?? null}
            dim={c?.game_status === 'final' && awayScore != null && homeScore != null && homeScore < awayScore}
          />
        </div>
        <div className={styles.statusLine}>
          {c && <StatusBadge status={c.game_status} />}
          {c?.series_description && c.series_description !== 'Regular Season' && (
            <span className={styles.inning}>{c.series_description}</span>
          )}
          {c?.double_header && c.double_header !== 'N' && c.game_number === 2 && (
            <span className={styles.inning}>Game 2</span>
          )}
          {(c?.away_probable_pitcher_name || c?.home_probable_pitcher_name) && (
            <span className={styles.inning}>
              SP {shortName(c?.away_probable_pitcher_name) || 'TBD'} vs{' '}
              {shortName(c?.home_probable_pitcher_name) || 'TBD'}
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

      {/* The real game, above every simulation (a live or final game). */}
      {(isLive || isFinal) && c && (
        <section className={styles.realGame} aria-label="The game" data-testid="real-game">
          <div className={styles.realMain}>
            <h2 className={styles.sectionTitle}>The game</h2>
            {realFeed.data?.linescore ? (
              <Linescore
                game={c}
                ls={realFeed.data.linescore}
                liveOffense={realFeed.data.live?.offense ?? null}
              />
            ) : (
              <p className={styles.muted}>{realFeed.error ?? 'Loading the game…'}</p>
            )}
            {isLive && realFeed.data?.live && <OnTheField game={c} feed={realFeed.data} />}
            {realFeed.data?.box && <BoxScore game={c} feed={realFeed.data} />}
          </div>
          <div className={styles.realSide}>
            <Card title="Play-by-play" count={realPlays.data?.plays.length}>
              {realPlays.error ? (
                <p className={styles.muted}>{realPlays.error}</p>
              ) : (
                <ActualPlayByPlay
                  plays={realPlays.data?.plays ?? []}
                  names={realPlays.data?.names ?? {}}
                  awayLabel={awayShort}
                  homeLabel={homeShort}
                  newestFirst={isLive}
                  selectedAtBat={whatIfAt ?? null}
                  onWhatIf={(atBat) => setWhatIfAt(atBat)}
                />
              )}
            </Card>
          </div>
        </section>
      )}

      {/* The what-if: from a play (live or final), or from first pitch. */}
      {c && c.game_status !== 'postponed' && (
        whatIfAt !== undefined || (!isLive && !isFinal) ? (
          <WhatIfPanel
            key={whatIfAt ?? 'first-pitch'}
            gamePk={gamePk}
            atBat={whatIfAt ?? null}
            awayAbbr={awayShort}
            homeAbbr={homeShort}
            onClose={isLive || isFinal ? () => setWhatIfAt(undefined) : undefined}
          />
        ) : (
          <p className={styles.whatIfHint}>
            Press “What if from here” on a play to change the hitter, the pitcher, a runner or
            the defense and simulate the rest of the game, or{' '}
            <button type="button" className={styles.linkButton} onClick={() => setWhatIfAt(null)}>
              start from first pitch
            </button>
            .
          </p>
        )
      )}

      {/* The simulation of the game from first pitch. */}
      <div className={styles.simColumn}>
        <SimulationCard
          gamePk={gamePk}
          onDone={(run) => {
            setRunId(run.run_id)
            // The run stored its representative game in the replay file.
            showStoredGame()
          }}
        />

        <Card title="Simulated game" headerActions={simButton}>
          {ls ? (
            <>
              <LinescoreGraphic
                away={{
                  name: awayShort,
                  byInning: ls.away_by_inning,
                  runs: ls.away_runs,
                  hits: ls.away_hits,
                  errors: ls.away_errors,
                }}
                home={{
                  name: homeShort,
                  byInning: ls.home_by_inning,
                  runs: ls.home_runs,
                  hits: ls.home_hits,
                  errors: ls.home_errors,
                }}
              />
              <p className={styles.caption}>
                One simulated game{replaySeed != null ? ` (seed ${replaySeed})` : ''}, not the real
                result.
              </p>
              <details className={styles.simPlays} data-testid="sim-plays">
                <summary>
                  Its play-by-play ({plays.data?.n_plate_appearances ?? 0} plate appearances)
                </summary>
                <PlayByPlayList
                  key={plays.data?.run_id ?? 'none'}
                  entries={plays.data?.entries ?? []}
                  names={plays.data?.names}
                  awayLabel={awayShort}
                  homeLabel={homeShort}
                  emptyText="No plays stored for this simulated game."
                />
              </details>
            </>
          ) : (
            <p className={styles.muted}>
              No simulated game yet. Press “Simulate a game”, or run a simulation above.
            </p>
          )}
          {simError && (
            <p className={styles.error} role="alert">
              {simError}
            </p>
          )}
        </Card>

        <Panel label="Projections" collapsible defaultOpen={false} storageKey="gamepage.projections">
          <BoxscorePanel
            gamePk={gamePk}
            awayLabel={away}
            homeLabel={home}
            onLoaded={showStoredGame}
            runId={runId}
          />
        </Panel>

        <Panel label="Betting" collapsible defaultOpen={false} storageKey="gamepage.betting">
          <BettingCard gamePk={gamePk} />
        </Panel>
      </div>
    </div>
  )
}
