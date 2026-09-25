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

/**
 * How the run line was priced (SIM-549). A `pair` is one bet with two sides
 * (the away spread is the negative of the home spread): the two prices de-vig
 * against each other. `two_bets` is two separate bets (e.g. home -1.5 and
 * away -1.5): each price is read over the game's two-way `reference_margin`.
 */
export interface RunLinePricing {
  shape: 'pair' | 'two_bets' | string
  home_line: number
  away_line: number
  reference_margin: number | null
  reference_source: string | null
}

export interface EdgesResponse {
  game_pk: number
  n_iterations: number
  base_seed: number | null
  markets: string[]
  /** market → "injected" | "mock" (where each market's prices came from). */
  odds_source: Record<string, string>
  edges: EdgeReport[]
  run_line_pricing?: RunLinePricing | null
}

export interface SignalsResponse {
  game_pk: number
  n_iterations: number
  base_seed: number | null
  config: Record<string, number>
  odds_source: Record<string, string>
  signals: BetSignal[]
  run_line_pricing?: RunLinePricing | null
}

// --- line-movement / CLV (SIM-396) ----------------------------------------

/** One timestamped quote for one side of a market (LineQuoteModel). */
export interface LineQuote {
  fetched_at: string | null
  line_type: string
  book: string
  is_sharp_book: boolean
  american: number
  other_american: number | null
  line: number | null
  implied_prob: number
  /** SIM-549: the other side's own spread (run lines only). */
  other_line?: number | null
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
  line_delta?: number | null
  implied_prob_series: number[]
  /** 'toward' | 'away' | 'flat' — where the price steamed for this side. */
  direction: string
  clv: Record<string, unknown> | null
  sharp_consensus: boolean | null
  has_movement: boolean
  beat_close: boolean
  /** SIM-549, run lines: 'pair' | 'two_bets' | 'mixed'. */
  run_line_shape?: string | null
  /** How the CLV was priced: 'pair' | 'two_bets' (an end was two separate bets). */
  clv_basis?: string | null
  /** Why there is no CLV, or a caveat on it. */
  clv_note?: string | null
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
