/**
 * slate.ts — the slate's order and refresh interval (SIM-519 Part A).
 */
import { cardStatus, type GameCard as GameCardRow, type GameStatus } from '@/api/games'

/** The slate's refresh interval while a game is live or the date is today. */
export const SLATE_POLL_MS = 30_000

const RANK: Record<GameStatus, number> = { live: 0, scheduled: 1, final: 2, postponed: 3 }

/** Live, then scheduled by first pitch, then final, then postponed (stable). */
export function sortSlate(games: GameCardRow[]): GameCardRow[] {
  return games
    .map((g, i) => ({ g, i }))
    .sort((a, b) => {
      const ra = RANK[cardStatus(a.g)]
      const rb = RANK[cardStatus(b.g)]
      if (ra !== rb) return ra - rb
      const ta = a.g.start_utc ? Date.parse(a.g.start_utc) : Number.POSITIVE_INFINITY
      const tb = b.g.start_utc ? Date.parse(b.g.start_utc) : Number.POSITIVE_INFINITY
      if (ta !== tb) return ta - tb
      return a.i - b.i
    })
    .map((x) => x.g)
}
