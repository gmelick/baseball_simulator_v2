/**
 * cardText.ts — the words on a Daily Diamond card's header (SIM-519): the
 * state tag, the line under the score and our sim line.
 */
import { cardStatus, type GameCard as GameCardRow, type GameStatus } from '@/api/games'

import { ageLabel, localStartTime, ordinal, parseIso } from './format'

/** The text of the state tag. */
export function statusTagText(game: GameCardRow, status: GameStatus = cardStatus(game)): string {
  if (status === 'live') {
    const half = game.inning_half ?? ''
    const word = half.startsWith('Top') ? 'Top' : half.startsWith('Bot') ? 'Bot' : half.startsWith('Mid') ? 'Mid' : half ? 'End' : ''
    const inning = game.inning != null ? `${word} ${ordinal(game.inning)}`.trim() : 'Live'
    if (game.outs == null || word === 'Mid' || word === 'End') return inning
    return `${inning} · ${game.outs} out${game.outs === 1 ? '' : 's'}`
  }
  if (status === 'final') {
    return game.n_innings != null && game.n_innings > 9 ? `Final/${game.n_innings}` : 'Final'
  }
  if (status === 'postponed') {
    const word = /^(Cancelled|Suspended)/.exec(game.detailed_state ?? '')?.[1] ?? 'PPD'
    return game.reason ? `${word} · ${game.reason}` : word
  }
  const time = game.start_time_tbd ? 'TBD' : localStartTime(game.start_utc) ?? 'Scheduled'
  return game.double_header && game.double_header !== 'N' && game.game_number === 2 ? `${time} · Game 2` : time
}

export function centerSub(game: GameCardRow, status: GameStatus): string {
  if (status === 'live') {
    const half = game.inning_half ?? ''
    const arrow = half.startsWith('Top') ? '▲' : half.startsWith('Bot') ? '▼' : ''
    return game.inning != null ? `${arrow} ${ordinal(game.inning)}`.trim() : 'Live'
  }
  if (status === 'final') return 'Final'
  if (status === 'postponed') {
    if (!game.rescheduled_to) return 'Postponed'
    const d = parseIso(game.rescheduled_to).toLocaleDateString([], { month: 'short', day: 'numeric' })
    return `→ ${d}`
  }
  return 'Pregame'
}

/** Our sim line: "SIM · NYY 54% · 2h ago", or "Not simulated". */
export function simLine(game: GameCardRow, status: GameStatus): string {
  const s = game.sim_summary
  if (!s) return 'Not simulated'
  const homeFav = s.home_win_pct >= s.away_win_pct
  const abbr = (homeFav ? game.home_team_abbrev : game.away_team_abbrev) ?? (homeFav ? 'Home' : 'Away')
  const pct = Math.round((homeFav ? s.home_win_pct : s.away_win_pct) * 100)
  const age = ageLabel(game.sim_run_at ?? s.simulated_at)
  const label = status === 'live' || status === 'final' ? 'Sim (pre-game)' : 'Sim'
  return [`${label} · ${abbr} ${pct}%`, age].filter(Boolean).join(' · ')
}
