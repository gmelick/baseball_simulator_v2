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

/** The card states. Since SIM-519 the server maps the league's status. */
export type GameStatus = 'scheduled' | 'live' | 'final' | 'postponed'

/** The array-free sim summary a card carries (GameSimSummaryLite). */
export interface SimSummaryLite {
  n_iterations: number
  home_win_pct: number
  away_win_pct: number
  home_score_mean: number
  away_score_mean: number
  simulated_at: string
}

/** One game from GET /api/games/{date}. SIM-519: the league's schedule says
 *  which games exist and their state; our database adds the lineup flag and
 *  the newest sim run. Every SIM-519 field is optional (an old mock omits it). */
export interface GameCard {
  game_pk: number
  season: number
  game_date: string
  /** The league's detailed state, else the stored `raw.games.status`. */
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
  lineup_ready?: boolean | null
  // SIM-519 Part A
  game_status?: GameStatus | null
  detailed_state?: string | null
  reason?: string | null
  /** ISO-8601 UTC first pitch; null when TBD. */
  start_utc?: string | null
  start_time_tbd?: boolean | null
  /** N (none), Y (straight) or S (split). */
  double_header?: string | null
  game_number?: number | null
  game_type?: string | null
  series_description?: string | null
  away_score?: number | null
  home_score?: number | null
  inning?: number | null
  inning_half?: string | null
  outs?: number | null
  n_innings?: number | null
  away_probable_pitcher_id?: number | null
  away_probable_pitcher_name?: string | null
  home_probable_pitcher_id?: number | null
  home_probable_pitcher_name?: string | null
  rescheduled_to?: string | null
  rescheduled_from?: string | null
  lineup_source?: string | null
  sim_summary?: SimSummaryLite | null
  sim_run_at?: string | null
  db_known?: boolean | null
}

/** Envelope from GET /api/games/{date}. */
export interface GamesOnDateResponse {
  date: string
  count: number
  games: GameCard[]
  /** schedule (fresh or cached), schedule_cached (last good copy) or db. */
  source?: 'schedule' | 'schedule_cached' | 'db' | null
  feed_error?: string | null
  fetched_at?: string | null
}

/** The card state of a row: the server's `game_status`, else a map of the
 *  stored status for an old payload that predates SIM-519. */
export function cardStatus(game: Pick<GameCard, 'game_status' | 'status'>): GameStatus {
  if (game.game_status) return game.game_status
  const raw = game.status ?? ''
  if (raw === 'Live' || raw === 'In Progress') return 'live'
  if (raw === 'Final' || raw === 'Game Over') return 'final'
  if (/^(Postponed|Suspended|Cancelled)/.test(raw)) return 'postponed'
  return 'scheduled'
}

// ---------------------------------------------------------------------------
// The card detail (GET /{game_pk}/feed — GameFeedCardModel, SIM-519 Part I)
// ---------------------------------------------------------------------------

export interface FeedPerson {
  id: number
  name: string
}

export interface FeedInning {
  num: number | null
  away: number | null
  home: number | null
}

export interface FeedTotals {
  runs: number | null
  hits: number | null
  errors: number | null
}

export interface FeedLinescore {
  innings: FeedInning[]
  away: FeedTotals
  home: FeedTotals
  home_did_not_bat_last: boolean
  current_inning: number | null
  inning_half: string | null
}

export interface FeedLineupSlot {
  order: number
  id: number | null
  name: string
  pos: string | null
}

export interface FeedBatterLine {
  id: number | null
  name: string
  pos: string | null
  batting_order: number | null
  is_sub: boolean
  ab: number | null
  r: number | null
  h: number | null
  rbi: number | null
  bb: number | null
  k: number | null
  hr: number | null
  avg: string | null
}

export interface FeedPitcherLine {
  id: number | null
  name: string
  outs: number | null
  ip: string | null
  h: number | null
  r: number | null
  er: number | null
  bb: number | null
  k: number | null
  np: number | null
  era: string | null
}

export interface FeedBoxSide {
  batters: FeedBatterLine[]
  pitchers: FeedPitcherLine[]
}

export interface FeedLive {
  balls: number | null
  strikes: number | null
  outs: number | null
  offense: 'away' | 'home' | null
  runners: { first: FeedPerson | null; second: FeedPerson | null; third: FeedPerson | null }
  batter: FeedPerson | null
  pitcher: (FeedPerson & { np: number | null }) | null
  fielders: Record<string, string | null>
  last_play: string | null
}

export interface GameFeedCard {
  game_pk: number | null
  status: GameStatus
  detailed_state: string | null
  linescore: FeedLinescore | null
  lineups: {
    away: FeedLineupSlot[]
    home: FeedLineupSlot[]
    away_probable_pitcher: FeedPerson | null
    home_probable_pitcher: FeedPerson | null
  }
  box: { away: FeedBoxSide; home: FeedBoxSide } | null
  live: FeedLive | null
  source: 'feed' | 'feed_cached'
  feed_error: string | null
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

// ---------------------------------------------------------------------------
// Linescore (GET /{game_pk}/linescore — LinescoreModel, SIM-362)
// ---------------------------------------------------------------------------

export interface InningLine {
  inning: number
  /** null = half not played ("x" on the scoreboard). */
  away: number | null
  home: number | null
  away_played: boolean
  home_played: boolean
}

export interface Linescore {
  innings: InningLine[]
  away_runs: number
  home_runs: number
  away_hits: number
  home_hits: number
  away_errors: number
  home_errors: number
  n_innings: number
  away_by_inning: Array<number | null>
  home_by_inning: Array<number | null>
}

// ---------------------------------------------------------------------------
// Play-by-play (GET /{game_pk}/plays — PlayByPlayModel, SIM-331)
// ---------------------------------------------------------------------------

export interface PlayByPlayEntry {
  sequence: number
  at_bat: number
  pitch: number
  pitch_outcome: string
  is_contact: boolean
  is_pa_end: boolean
  event: string | null
  runs_scored: number
  outs_recorded: number
  exit_velo: number | null
  launch_angle: number | null
  spray_angle: number | null
  runs: number
  canonical_event: string | null
  /** SIM-561: the inning, half, outs and batter BEFORE the pitch; null on an
   *  older stored stream. */
  inning?: number | null
  half?: 'top' | 'bottom' | null
  outs_before?: number | null
  batter_id?: number | null
  /** SIM-561: the pitcher who threw it. */
  pitcher_id?: number | null
  /** SIM-561: the score AFTER the pitch. */
  away_score?: number | null
  home_score?: number | null
}

export interface PlayByPlay {
  entries: PlayByPlayEntry[]
  n_pitches: number
  n_plate_appearances: number
  /** SIM-561: the stored run these plays belong to, and its seed. */
  run_id?: number | null
  base_seed?: number | null
  /** SIM-561: str(player_id) → name, for every batter and pitcher. */
  names?: Record<string, string>
}

/** SIM-561: POST /{game_pk}/sample-game — the stored game's run id and seed. */
export interface SampleGame {
  game_pk: number
  run_id: number
  base_seed: number
}

// ---------------------------------------------------------------------------
// Live state (GET /{game_pk}/live — LiveGameStateResponse, SIM-386)
// ---------------------------------------------------------------------------

export interface LiveState {
  game_pk: number
  session_id: string
  inning: number
  half: string
  outs: number
  balls: number
  strikes: number
  home_score: number
  away_score: number
  batting_team_id: number | null
  fielding_team_id: number | null
  on_1b: number | null
  on_2b: number | null
  on_3b: number | null
  current_batter_id: number | null
  current_pitcher_id: number | null
  home_lineup: number[]
  away_lineup: number[]
  home_bullpen: number[]
  away_bullpen: number[]
  home_bench: number[]
  away_bench: number[]
  updated_at: string | null
}

// ---------------------------------------------------------------------------
// Boxscore card (GET /{game_pk}/boxscore — BoxscoreCardModel, SIM-366)
// ---------------------------------------------------------------------------

export interface BoxscoreRow {
  player_id: number
  /** prop_name → mean over the run (e.g. {"K": 6.4} pitcher, {"H": 1.2} batter). */
  means: Record<string, number>
  /** SIM-560: the player's full name; null when the lookup has none. */
  name?: string | null
  /** SIM-560: 'away' or 'home'; null for a player the game does not name. */
  side?: 'away' | 'home' | null
  /** SIM-560: the 1-9 batting-order slot; null for a player who does not bat. */
  lineup_slot?: number | null
  /** SIM-560: true for each side's starting pitcher. */
  starting_pitcher?: boolean
}

export interface BoxscoreCard {
  n_iterations: number
  /** SIM-560: the run's seed; pass it to fetchPropEdge to read the same run. */
  base_seed?: number | null
  /** str(player_id) → row. */
  players: Record<string, BoxscoreRow>
}

// ---------------------------------------------------------------------------
// Prop edge (GET /{game_pk}/props/{player_id}/{prop} — PropEdgeResponse, SIM-390)
// ---------------------------------------------------------------------------

export interface EdgeReport {
  label: string
  side: string
  line: number | null
  sim_prob: number
  market_fair_prob: number
  edge: number
  ev: number
  offered_american: number
  sim_fair_american: number
  clv: Record<string, unknown> | null
  positive_edge: boolean
  /**
   * SIM-555: the stored book label of the offered price (e.g. `bp:10`), when
   * the market was priced from the stored lines; null for an injected or mock price.
   */
  price_book?: string | null
  /** SIM-555: that book's display name (e.g. "FanDuel"). */
  price_book_name?: string | null
}

export interface PropEdge {
  player_id: number
  prop: string
  n: number
  support: number[]
  probabilities: number[]
  mean: number
  median: number
  std: number
  pmf: Record<string, number>
  line: number | null
  p_over: number | null
  p_under: number | null
  p_push: number | null
  edge_report: EdgeReport | null
}

export interface PropEdgeQuery {
  line?: number
  overMl?: number
  underMl?: number
  betSide?: 'over' | 'under'
  nIterations?: number
  baseSeed?: number
  /** SIM-519: read this stored run instead of running a batch. */
  runId?: number | null
}

// ---------------------------------------------------------------------------
// Roster override (POST /{game_pk}/simulate/with_override — SIM-358/388)
// ---------------------------------------------------------------------------

export interface SubstitutionSlot {
  /** 1-indexed lineup slot (1–9). */
  batting_order: number
  player_id: number
  side: 'home' | 'away'
}

export interface RosterOverride {
  home_lineup?: number[] | null
  away_lineup?: number[] | null
  /** Targeted single-player substitutions (SIM-388). */
  substitutions?: SubstitutionSlot[] | null
  pitcher_id?: number | null
  bat_hand?: string | null
  description?: string | null
}

export interface MetricDelta {
  metric: string
  baseline: number
  override: number
  /** override − baseline. */
  delta: number
}

export interface OverrideDelta {
  metrics: Record<string, MetricDelta>
  description: string | null
}

export interface WithOverrideResponse {
  game_pk: number
  n_iterations: number
  base_seed: number | null
  baseline: Record<string, unknown>
  override: Record<string, unknown>
  delta: OverrideDelta
}

/** Raised for any non-2xx games-API response, carrying the HTTP status. */
export class GamesApiError extends Error {
  readonly status: number
  /** Seconds from a Retry-After header (a 503 for an unpublished lineup). */
  readonly retryAfter: number | null
  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message)
    this.name = 'GamesApiError'
    this.status = status
    this.retryAfter = retryAfter
  }
}

function retryAfterOf(res: Response): number | null {
  const v = Number(res.headers.get('Retry-After'))
  return Number.isFinite(v) && v > 0 ? v : null
}

export async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include' })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<T>
}

async function postJson<T>(url: string, payload: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`, retryAfterOf(res))
  }
  return res.json() as Promise<T>
}

/** GET /api/games/{date} — the slate for a YYYY-MM-DD date. */
export function fetchGamesOnDate(date: string): Promise<GamesOnDateResponse> {
  return getJson<GamesOnDateResponse>(`/api/games/${encodeURIComponent(date)}`)
}

/** GET /api/games/{game_pk}/feed — the real game behind a slate card (SIM-519 Part I). */
export function fetchGameFeed(gamePk: number): Promise<GameFeedCard> {
  return getJson<GameFeedCard>(`/api/games/${gamePk}/feed`)
}

/** GET /api/games/{game_pk}/status — one game's aggregate card. */
export function fetchGameCard(gamePk: number): Promise<GameCardAggregate> {
  return getJson<GameCardAggregate>(`/api/games/${gamePk}/status`)
}

/** GET /api/games/{game_pk}/linescore — per-inning R/H/E grid (needs a persisted sim). */
export function fetchLinescore(gamePk: number): Promise<Linescore> {
  return getJson<Linescore>(`/api/games/${gamePk}/linescore`)
}

/** GET /api/games/{game_pk}/plays — pitch-by-pitch scroll (needs a persisted sim).
 *  SIM-561: `runId` reads that stored game; omit it for the newest. */
export function fetchPlays(gamePk: number, runId?: number | null): Promise<PlayByPlay> {
  const qs = runId != null ? `?run_id=${runId}` : ''
  return getJson<PlayByPlay>(`/api/games/${gamePk}/plays${qs}`)
}

/** SIM-561: GET /api/games/{game_pk}/card — the newest stored simulated game's
 *  linescore, its run id and seed. Read the plays with that run id, so the two
 *  panels always show one game. */
export interface ReplayCard {
  game_pk: number
  linescore: Linescore
  run_id: number | null
  base_seed: number | null
}

export function fetchReplayCard(gamePk: number): Promise<ReplayCard> {
  return getJson<ReplayCard>(`/api/games/${gamePk}/card`)
}

/** GET /api/games/{game_pk}/live — live in-progress state (404 when not live). */
export function fetchLiveState(gamePk: number): Promise<LiveState> {
  return getJson<LiveState>(`/api/games/${gamePk}/live`)
}

/** POST /api/games/{game_pk}/simulate/with_override — baseline-vs-override diff (SIM-358/388). */
export function postWithOverride(
  gamePk: number,
  override: RosterOverride,
  opts: { nIterations?: number; baseSeed?: number } = {},
): Promise<WithOverrideResponse> {
  const params = new URLSearchParams()
  if (opts.nIterations != null) params.set('n_iterations', String(opts.nIterations))
  if (opts.baseSeed != null) params.set('base_seed', String(opts.baseSeed))
  const qs = params.toString()
  return postJson<WithOverrideResponse>(
    `/api/games/${gamePk}/simulate/with_override${qs ? `?${qs}` : ''}`,
    override,
  )
}

/** POST /api/games/{game_pk}/sample-game — simulate and store one game (SIM-561).
 *  The linescore, decisions and play-by-play endpoints then serve it. */
export function postSampleGame(gamePk: number, baseSeed?: number): Promise<SampleGame> {
  const qs = baseSeed != null ? `?base_seed=${baseSeed}` : ''
  return postJson<SampleGame>(`/api/games/${gamePk}/sample-game${qs}`, {})
}

/** GET /api/games/{game_pk}/boxscore — per-player prop means over N iterations.
 *  SIM-560: a seeded run is cached, so fetchPropEdge with the same seed and N
 *  reads the same run. */
export function fetchBoxscore(
  gamePk: number,
  nIterations?: number,
  baseSeed?: number,
  runId?: number | null,
): Promise<BoxscoreCard> {
  const params = new URLSearchParams()
  if (runId != null) params.set('run_id', String(runId))
  else {
    if (nIterations != null) params.set('n_iterations', String(nIterations))
    if (baseSeed != null) params.set('base_seed', String(baseSeed))
  }
  const qs = params.toString()
  return getJson<BoxscoreCard>(`/api/games/${gamePk}/boxscore${qs ? `?${qs}` : ''}`)
}

// ---------------------------------------------------------------------------
// One simulation run per game (SIM-519 Part E)
// ---------------------------------------------------------------------------

export type SimRunStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'

export interface SimRun {
  run_id: number
  game_pk: number
  status: SimRunStatus
  n_iterations: number
  progress_done: number
  base_seed: number | null
  position_in_queue: number | null
  existing: boolean
  requested_at: string | null
  started_at: string | null
  finished_at: string | null
  error: string | null
  lineup_source: string | null
  bullpen_source: string | null
  replay_run_id: number | null
  summary: SimSummaryLite | null
}

/** POST /api/games/{game_pk}/simulate — queue a run (or get the active one). */
export function startSimRun(gamePk: number, nIterations: number): Promise<SimRun> {
  return postJson<SimRun>(`/api/games/${gamePk}/simulate`, { n_iterations: nIterations })
}

/** GET /api/games/{game_pk}/simulate/runs/latest — 404 when the game has none. */
export function fetchLatestRun(gamePk: number): Promise<SimRun> {
  return getJson<SimRun>(`/api/games/${gamePk}/simulate/runs/latest`)
}

/** GET /api/games/{game_pk}/simulate/runs/{run_id}. */
export function fetchSimRun(gamePk: number, runId: number): Promise<SimRun> {
  return getJson<SimRun>(`/api/games/${gamePk}/simulate/runs/${runId}`)
}

/** DELETE /api/games/{game_pk}/simulate/runs/{run_id} — cancel. */
export async function cancelSimRun(gamePk: number, runId: number): Promise<SimRun> {
  const res = await fetch(`/api/games/${gamePk}/simulate/runs/${runId}`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<SimRun>
}

/** GET /api/games/{game_pk}/props/{player_id}/{prop} — full PMF (+ optional
 *  over/under + edge report when a line and odds are supplied). */
export function fetchPropEdge(
  gamePk: number,
  playerId: number,
  prop: string,
  q: PropEdgeQuery = {},
): Promise<PropEdge> {
  const params = new URLSearchParams()
  if (q.line != null) params.set('line', String(q.line))
  if (q.overMl != null) params.set('over_ml', String(q.overMl))
  if (q.underMl != null) params.set('under_ml', String(q.underMl))
  if (q.betSide != null) params.set('bet_side', q.betSide)
  if (q.runId != null) params.set('run_id', String(q.runId))
  else {
    if (q.nIterations != null) params.set('n_iterations', String(q.nIterations))
    if (q.baseSeed != null) params.set('base_seed', String(q.baseSeed))
  }
  const qs = params.toString()
  return getJson<PropEdge>(
    `/api/games/${gamePk}/props/${playerId}/${encodeURIComponent(prop)}${qs ? `?${qs}` : ''}`,
  )
}
