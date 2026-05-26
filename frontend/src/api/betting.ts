/**
 * betting.ts — SIM-395/396
 * Client for the /api/betting surface (edges, signals, line-movement, CLV).
 *
 * Mirrors the FastAPI response models:
 *   - EdgesResponse   → api/routes/betting.py (SIM-367)
 *   - SignalsResponse → SIM-369
 *   - LineMovementResponse / ClvSnapshotResponse → SIM-368 (used by SIM-396)
 *
 * Reuses the shared EdgeReport shape from the games client and the same
 * credentials/error conventions.
 */
import { type EdgeReport, GamesApiError } from './games'

export type { EdgeReport }

/** A fireable +EV recommendation (BetSignalModel, SIM-369). */
export interface BetSignal {
  label: string
  side: string
  line: number | null
  offered_american: number
  edge: number
  ev: number
  stake_fraction: number
  confidence: number
  rank: number
  report: EdgeReport
}

export interface EdgesResponse {
  game_pk: number
  n_iterations: number
  base_seed: number | null
  markets: string[]
  /** market → "injected" | "mock" (where each market's prices came from). */
  odds_source: Record<string, string>
  edges: EdgeReport[]
}

export interface SignalsResponse {
  game_pk: number
  n_iterations: number
  base_seed: number | null
  config: Record<string, number>
  odds_source: Record<string, string>
  signals: BetSignal[]
}

// --- line-movement / CLV (SIM-396) ----------------------------------------

export interface LineQuote {
  american: number | null
  implied_prob: number | null
  line: number | null
  ts: string | null
  is_closing: boolean
}

export interface LineMovement {
  game_pk: number
  market_type: string
  side: string
  book: string | null
  quotes: LineQuote[]
  opening_american: number | null
  closing_american: number | null
  opening_implied_prob: number | null
  closing_implied_prob: number | null
  implied_prob_series: number[]
  direction: string
  clv: Record<string, unknown> | null
  sharp_consensus: boolean | null
  has_movement: boolean
  beat_close: boolean
}

export interface LineMovementResponse {
  game_pk: number
  market_type: string
  book: string | null
  count: number
  series: LineMovement[]
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include' })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<T>
}

/** GET /api/betting/games/{game_pk}/edges */
export function fetchEdges(gamePk: number, nIterations = 200): Promise<EdgesResponse> {
  return getJson<EdgesResponse>(`/api/betting/games/${gamePk}/edges?n_iterations=${nIterations}`)
}

/** GET /api/betting/games/{game_pk}/signals */
export function fetchSignals(gamePk: number, nIterations = 200): Promise<SignalsResponse> {
  return getJson<SignalsResponse>(`/api/betting/games/${gamePk}/signals?n_iterations=${nIterations}`)
}

/** GET /api/betting/games/{game_pk}/line-movement */
export function fetchLineMovement(
  gamePk: number,
  marketType: string,
): Promise<LineMovementResponse> {
  return getJson<LineMovementResponse>(
    `/api/betting/games/${gamePk}/line-movement?market_type=${encodeURIComponent(marketType)}`,
  )
}
