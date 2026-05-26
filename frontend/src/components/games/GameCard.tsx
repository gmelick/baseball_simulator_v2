/**
 * GameCard.tsx — SIM-391
 *
 * One game in the Day Summary slate, rendered in a status-aware 3-state form
 * (scheduled / live / final; postponed is a muted fourth state). Shows the
 * away-vs-home matchup with team names/abbreviations and season-to-date W/L
 * records, plus a status Badge (LIVE pulses). Scores render when supplied
 * (live/final). The whole card is a link to the game page (SIM-393).
 *
 * Built from a `GameCard` row (api/games.ts). Score props are optional so the
 * Day Summary can render the slate from the single list call without an N+1
 * per-game fetch; a caller with the aggregate `/status` payload may pass them.
 */
import React from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui'
import type { GameCard as GameCardRow } from '@/api/games'
import { rawStatusToGameStatus, type GameStatus } from '@/api/games'

import styles from './GameCard.module.css'

export interface GameCardProps {
  game: GameCardRow
  /** Live/final away-team runs (optional; from the aggregate /status payload). */
  awayScore?: number | null
  /** Live/final home-team runs (optional). */
  homeScore?: number | null
}

function StatusBadge({ status }: { status: GameStatus }): React.ReactElement {
  switch (status) {
    case 'live':
      return (
        <Badge variant="live" pulse aria-label="Game in progress">
          LIVE
        </Badge>
      )
    case 'final':
      return <Badge variant="final">FINAL</Badge>
    case 'postponed':
      return <Badge variant="default">PPD</Badge>
    case 'scheduled':
    default:
      return <Badge variant="scheduled">SCHEDULED</Badge>
  }
}

function teamLabel(name: string | null, abbrev: string | null, id: number | null): string {
  return name ?? abbrev ?? (id != null ? `Team ${id}` : 'TBD')
}

function recordLabel(wins: number | null, losses: number | null): string | null {
  if (wins == null || losses == null) return null
  return `${wins}-${losses}`
}

/** One team's row inside the card (name + record on the left, score on the right). */
function TeamRow({
  name,
  record,
  score,
  winner,
}: {
  name: string
  record: string | null
  score?: number | null
  winner: boolean
}): React.ReactElement {
  return (
    <div className={`${styles.teamRow} ${winner ? styles.winner : ''}`}>
      <span className={styles.teamName}>{name}</span>
      {record != null && <span className={styles.record}>{record}</span>}
      {score != null && <span className={styles.score}>{score}</span>}
    </div>
  )
}

export function GameCard({ game, awayScore, homeScore }: GameCardProps): React.ReactElement {
  const status = rawStatusToGameStatus(game.status)
  const settled = status === 'final'
  // Winner highlight only once the game is final and both scores are known.
  const awayWon = settled && awayScore != null && homeScore != null && awayScore > homeScore
  const homeWon = settled && awayScore != null && homeScore != null && homeScore > awayScore

  return (
    <Link
      to={`/game/${game.game_pk}`}
      className={`${styles.card} ${styles[`status-${status}`]}`}
      aria-label={`${teamLabel(game.away_team_name, game.away_team_abbrev, game.away_team_id)} at ${teamLabel(
        game.home_team_name,
        game.home_team_abbrev,
        game.home_team_id,
      )} — ${status}`}
    >
      <div className={styles.cardHeader}>
        <span className={styles.venue}>{game.venue_name ?? ' '}</span>
        <StatusBadge status={status} />
      </div>

      <div className={styles.matchup}>
        <TeamRow
          name={teamLabel(game.away_team_name, game.away_team_abbrev, game.away_team_id)}
          record={recordLabel(game.away_wins, game.away_losses)}
          score={awayScore}
          winner={awayWon}
        />
        <TeamRow
          name={teamLabel(game.home_team_name, game.home_team_abbrev, game.home_team_id)}
          record={recordLabel(game.home_wins, game.home_losses)}
          score={homeScore}
          winner={homeWon}
        />
      </div>
    </Link>
  )
}
