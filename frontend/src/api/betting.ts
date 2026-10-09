/**
 * betting.ts — SIM-395/396
 * Client for the /api/betting surface (edges, signals, the slate card's lines).
 *
 * Mirrors the FastAPI response models:
 *   - EdgesResponse   → api/routes/betting.py (SIM-367)
 *   - SignalsResponse → SIM-369
 *
 * Reuses the shared EdgeReport shape from the games client and the same
 * credentials/error conventions.
 */
import { type EdgeReport, GamesApiError } from './games'

export type { EdgeReport }

/** A fireable +EV recommendation (BetSignalModel, SIM-369). */
export interface BetSignal {
  label: string
  /** 'home' | 'away' | 'over' | 'under', or 'draw' (SIM-546: a three-way market's tie). */
  side: string
  line: number | null
  offered_american: number
  edge: number
  ev: number
  stake_fraction: number
  confidence: number
  rank: number
  report: EdgeReport
  /** SIM-555: the stored book label of the offered price (null: injected or mock). */
  price_book?: string | null
  /** SIM-555: that book's display name (e.g. "FanDuel"). */
  price_book_name?: string | null
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
  /** The real final once the game is over; each edge's `result` grades its side. */
  final?: { away: number; home: number } | null
  /**
   * The market types priced, in the vocabulary order (SIM-546: up to fifteen).
   * A type is the report label except the full-game run line: 'runline' here,
   * 'run_line' on its edges.
   */
  markets: string[]
  /** market → "injected" | "stored" | "mock" (where each market's prices came from). */
  odds_source: Record<string, string>
  edges: EdgeReport[]
  run_line_pricing?: RunLinePricing | null
  /**
   * SIM-555: market → the stored label of the graded book, the one book whose
   * row gave the fair probability (stored markets only).
   */
  fair_book?: Record<string, string>
  /** SIM-555: market → that book's display name (e.g. "DraftKings"). */
  fair_book_name?: Record<string, string>
  /**
   * SIM-546: report label → the market's plain name ("First five total"). The
   * betting card titles its sections from it.
   */
  market_names?: Record<string, string>
  /**
   * SIM-546: report label → how that run line was priced, for every priced run
   * line ("run_line", "f1_runline", "f5_runline").
   */
  run_line_pricing_by_label?: Record<string, RunLinePricing>
}

export interface SignalsResponse {
  game_pk: number
  n_iterations: number
  base_seed: number | null
  config: Record<string, number>
  /** market → "injected" | "stored" | "mock". */
  odds_source: Record<string, string>
  signals: BetSignal[]
  run_line_pricing?: RunLinePricing | null
  /** SIM-555: market → the graded book's stored label (stored markets only). */
  fair_book?: Record<string, string>
  /** SIM-555: market → that book's display name. */
  fair_book_name?: Record<string, string>
  /** SIM-546: report label → the market's plain name ("First five total"). */
  market_names?: Record<string, string>
  /** SIM-546: report label → how that run line was priced (every priced run line). */
  run_line_pricing_by_label?: Record<string, RunLinePricing>
}

// --- the shared GET helper ------------------------------------------------

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include' })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// The slate card's lines (GET /games/{pk}/card-odds — CardOddsResponse, SIM-519 Part I)
// ---------------------------------------------------------------------------

export interface CardOdds {
  game_pk: number
  moneyline: { book: string; line_type: string; away: number; home: number } | null
  runline: {
    book: string
    line_type: string
    away: { line: number; price: number }
    home: { line: number; price: number }
  } | null
  total: { book: string; line_type: string; line: number; over: number; under: number } | null
  settled: {
    away_score: number
    home_score: number
    moneyline: 'away' | 'home' | null
    runline: 'away' | 'home' | 'push' | null
    total: 'over' | 'under' | 'push' | null
  } | null
}

/** GET /api/betting/games/{game_pk}/card-odds — 404 when the game has no stored line. */
export function fetchCardOdds(gamePk: number): Promise<CardOdds> {
  return getJson<CardOdds>(`/api/betting/games/${gamePk}/card-odds`)
}

/** GET /api/betting/games/{game_pk}/edges */
export function fetchEdges(gamePk: number, nIterations = 200): Promise<EdgesResponse> {
  return getJson<EdgesResponse>(`/api/betting/games/${gamePk}/edges?n_iterations=${nIterations}`)
}

/** GET /api/betting/games/{game_pk}/signals */
export function fetchSignals(gamePk: number, nIterations = 200): Promise<SignalsResponse> {
  return getJson<SignalsResponse>(`/api/betting/games/${gamePk}/signals?n_iterations=${nIterations}`)
}
