/**
 * games.ts — SIM-391
 * Browser-side client for the games surface.
 *
 * Mirrors the auth client (credentials:'include' so the httpOnly `sim_session`
 * cookie rides along) and the Vite dev proxy forwards /api/* to localhost:8000.
 *
 * Types are hand-mirrored from the FastAPI response models:
 *   - GameCard / GamesOnDateResponse      → api/routes/games.py (SIM-355 + SIM-383)
 *   - GameCardAggregate                   → GameCardAggregateResponse (SIM-384)
 * Keep them in sync when those models change.
 */

/** The 3-state status enum the UI cares about (server maps 8 raw values → these). */
export type GameStatus = 'scheduled' | 'live' | 'final' | 'postponed'

/** One game row from GET /api/games/{date} (SIM-383 enriched). */
export interface GameCard {
  game_pk: number
  season: number
  game_date: string
  /** Raw `raw.games.status` (e.g. "Final", "Preview"); null on sparse rows. */
  status: string | null
  home_team_id: number | null
  away_team_id: number | null
  venue_id: number | null
  // SIM-383 enrichment
  home_team_name: string | null
  home_team_abbrev: string | null
  away_team_name: string | null
  away_team_abbrev: string | null
  venue_name: string | null
  venue_city: string | null
  home_wins: number | null
  home_losses: number | null
  away_wins: number | null
  away_losses: number | null
}

/** Envelope from GET /api/games/{date}. */
export interface GamesOnDateResponse {
  date: string
  count: number
  games: GameCard[]
}

/** Map a raw `raw.games.status` value → the 3-state UI status.
 *  Mirrors the server's `_RAW_STATUS_TO_GAME_STATUS` (api/routes/games.py).
 *  The list endpoint returns the raw status; the aggregate `/status` endpoint
 *  returns the already-mapped value. */
export function rawStatusToGameStatus(raw: string | null | undefined): GameStatus {
  switch (raw) {
    case 'Live':
      return 'live'
    case 'Final':
      return 'final'
    case 'Postponed':
    case 'Suspended':
    case 'Cancelled':
      return 'postponed'
    case 'Preview':
    case 'Warmup':
    case 'Pre-Game':
    default:
      return 'scheduled'
  }
}

/** Aggregate card from GET /api/games/{game_pk}/status (SIM-384). */
export interface GameCardAggregate {
  game_pk: number
  /** Mapped 3-state status: scheduled | live | final | postponed. */
  game_status: GameStatus
  game_date: string
  season: number
  home_team_id: number | null
  away_team_id: number | null
  venue_id: number | null
  home_team_name: string | null
  home_team_abbrev: string | null
  away_team_name: string | null
  away_team_abbrev: string | null
  venue_name: string | null
  venue_city: string | null
  home_wins: number | null
  home_losses: number | null
  away_wins: number | null
  away_losses: number | null
  home_score_final: number | null
  away_score_final: number | null
  /** Most-recent persisted Monte-Carlo summary; null when none run yet. */
  sim_summary: Record<string, unknown> | null
  odds: null
}

/** Raised for any non-2xx games-API response, carrying the HTTP status. */
export class GamesApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'GamesApiError'
    this.status = status
  }
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include' })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<T>
}

/** GET /api/games/{date} — the slate for a YYYY-MM-DD date. */
export function fetchGamesOnDate(date: string): Promise<GamesOnDateResponse> {
  return getJson<GamesOnDateResponse>(`/api/games/${encodeURIComponent(date)}`)
}

/** GET /api/games/{game_pk}/status — one game's aggregate card. */
export function fetchGameCard(gamePk: number): Promise<GameCardAggregate> {
  return getJson<GameCardAggregate>(`/api/games/${gamePk}/status`)
}
