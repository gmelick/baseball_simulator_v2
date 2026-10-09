/**
 * GameCard.tsx — the Daily Diamond slate card (SIM-519; design 2026-10-08).
 *
 * The header shows the state tag, both clubs (logo, colour chip, name,
 * record) and the score or "vs". A click on the header opens the card in
 * place; the body (CardDetail) then reads the real game from the league feed
 * and the book's lines. The state comes from the server (`game_status`), so
 * the card never maps a raw status itself.
 */
import React from 'react'

import { cardStatus, type GameCard as GameCardRow, type GameStatus } from '@/api/games'
import { CardDetail } from '@/components/slate/CardDetail'
import { TeamBlock } from '@/components/slate/TeamBlock'
import { centerSub, simLine, statusTagText } from '@/components/slate/cardText'

import styles from './GameCard.module.css'

export interface GameCardProps {
  game: GameCardRow
  open: boolean
  onToggle: () => void
  /** Bumped by the slate's refresh; an open live card re-reads its feed. */
  tick?: number
}

const TAG_CLASS: Record<GameStatus, string> = {
  live: styles.tagLive,
  final: styles.tagFinal,
  scheduled: styles.tagScheduled,
  postponed: styles.tagPostponed,
}

export function GameCard({ game, open, onToggle, tick = 0 }: GameCardProps): React.ReactElement {
  const status = cardStatus(game)
  const played = status === 'live' || status === 'final'
  const away = game.away_score ?? null
  const home = game.home_score ?? null
  const awayLost = status === 'final' && away != null && home != null && away < home
  const homeLost = status === 'final' && away != null && home != null && home < away
  const awayName = game.away_team_name ?? game.away_team_abbrev ?? 'Away'
  const homeName = game.home_team_name ?? game.home_team_abbrev ?? 'Home'
  const detailId = `card-detail-${game.game_pk}`

  return (
    <article className={styles.card} data-status={status} data-testid={`game-card-${game.game_pk}`}>
      <button
        type="button"
        className={styles.head}
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={detailId}
        aria-label={`${awayName} at ${homeName}, ${statusTagText(game, status)}. ${open ? 'Close' : 'Open'} the game.`}
      >
        <div className={styles.topRow}>
          <span className={`${styles.tag} ${TAG_CLASS[status]}`}>
            <span className={styles.dot} aria-hidden="true" />
            {statusTagText(game, status)}
          </span>
          {game.series_description && game.game_type !== 'R' && (
            <span className={styles.series}>{game.series_description}</span>
          )}
          <span className={`${styles.chevron} ${open ? styles.chevronOpen : ''}`} aria-hidden="true">
            ▼
          </span>
        </div>

        <div className={styles.matchup}>
          <TeamBlock
            side="away"
            teamId={game.away_team_id}
            abbr={game.away_team_abbrev}
            name={awayName}
            wins={game.away_wins}
            losses={game.away_losses}
            dim={awayLost}
          />
          <div className={styles.center}>
            <div className={styles.score}>{played && away != null && home != null ? `${away} – ${home}` : 'vs'}</div>
            <div className={styles.centerSub}>{centerSub(game, status)}</div>
          </div>
          <TeamBlock
            side="home"
            teamId={game.home_team_id}
            abbr={game.home_team_abbrev}
            name={homeName}
            wins={game.home_wins}
            losses={game.home_losses}
            dim={homeLost}
          />
        </div>

        <div className={styles.simLine} data-testid="sim-line">
          {simLine(game, status)}
        </div>
      </button>

      {open && (
        <div id={detailId} className={styles.body}>
          <CardDetail game={game} status={status} tick={tick} />
        </div>
      )}
    </article>
  )
}
