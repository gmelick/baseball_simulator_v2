/**
 * lab.ts — SIM-439
 * Browser-side client for the Data Lab surface: summary analytics, the schema
 * tree, the read-only SQL runner, and the AI-assistant capability probe.
 *
 * Mirrors the games.ts client (credentials:'include' so the httpOnly session
 * cookie rides along; the Vite dev proxy forwards /api/* to :8000). Response
 * types are hand-mirrored from the FastAPI models in api/routes/{analytics,
 * schema_introspect,sql_runner,ai_assistant}.py — keep them in sync.
 */
import { GamesApiError } from '@/api/games'

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'include' })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<T>
}

async function postJson<T>(url: string, payload: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<T>
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

export interface Kpis {
  total_pitches: number
  total_pitches_estimated: boolean
  total_games: number
  seasons: number[]
  first_date: string | null
  last_date: string | null
}

export interface SeasonRow {
  season: number
  games: number
  pitches: number
}
export interface PitchTypeRow {
  pitch_type: string
  n: number
  usage_pct: number | null
  avg_velo: number | null
  avg_ivb: number | null
  avg_hb: number | null
  avg_spin: number | null
}
export interface OutcomeRow {
  event: string
  n: number
}
export interface CountStateRow {
  balls: number
  strikes: number
  pitches: number
  swing_pct: number | null
  whiff_pct: number | null
}
export interface VeloBucketRow {
  velo_bucket: number
  n: number
}
export interface ZoneRow {
  zone: number
  pitches: number
  whiff_pct: number | null
  called_strike_pct: number | null
}

export type LeaderRow = Record<string, number | string | null>

export interface AnalyticsFilter {
  season: number
  pitcher?: number | null
  batter?: number | null
  stand?: string | null
  pThrows?: string | null
}

export const fetchKpis = (): Promise<Kpis> => getJson<Kpis>('/api/analytics/kpis')
export const fetchBySeason = (): Promise<SeasonRow[]> => getJson<SeasonRow[]>('/api/analytics/by-season')

export const fetchPitchTypeMix = (f: AnalyticsFilter): Promise<PitchTypeRow[]> =>
  getJson<PitchTypeRow[]>(`/api/analytics/pitch-type-mix${qs({ season: f.season, pitcher: f.pitcher, stand: f.stand })}`)

export const fetchPaOutcomes = (f: AnalyticsFilter): Promise<OutcomeRow[]> =>
  getJson<OutcomeRow[]>(`/api/analytics/pa-outcomes${qs({ season: f.season, batter: f.batter, p_throws: f.pThrows })}`)

export const fetchCountState = (f: AnalyticsFilter): Promise<CountStateRow[]> =>
  getJson<CountStateRow[]>(`/api/analytics/count-state${qs({ season: f.season, pitcher: f.pitcher, batter: f.batter })}`)

export const fetchVeloDistribution = (f: AnalyticsFilter & { pitchType?: string }): Promise<VeloBucketRow[]> =>
  getJson<VeloBucketRow[]>(
    `/api/analytics/velo-distribution${qs({ season: f.season, pitcher: f.pitcher, pitch_type: f.pitchType })}`,
  )

export const fetchZone = (f: AnalyticsFilter): Promise<ZoneRow[]> =>
  getJson<ZoneRow[]>(`/api/analytics/zone${qs({ season: f.season, pitcher: f.pitcher })}`)

export const fetchPitcherLeaders = (season: number, metric: string, minPa = 200, limit = 50): Promise<LeaderRow[]> =>
  getJson<LeaderRow[]>(`/api/analytics/leaderboard/pitchers${qs({ season, metric, min_pa: minPa, limit })}`)

export const fetchBatterLeaders = (
  season: number,
  metric: string,
  minPa = 200,
  limit = 50,
  vsHand?: string,
): Promise<LeaderRow[]> =>
  getJson<LeaderRow[]>(`/api/analytics/leaderboard/batters${qs({ season, metric, min_pa: minPa, limit, vs_hand: vsHand })}`)

// ---------------------------------------------------------------------------
// Data freshness (existing endpoint)
// ---------------------------------------------------------------------------

export interface DataFreshness {
  last_ingest_at: string | null
  latest_game_date: string | null
  total_games: number
  total_pitches: number
  has_data: boolean
  seasons: { season: number; games: number; pitches: number }[]
}
export const fetchFreshness = (): Promise<DataFreshness> => getJson<DataFreshness>('/api/data/freshness')

// ---------------------------------------------------------------------------
// Schema tree
// ---------------------------------------------------------------------------

export interface SchemaColumn {
  name: string
  data_type: string
}
export interface SchemaTable {
  name: string
  columns: SchemaColumn[]
}
export interface SchemaResponse {
  schemas: { name: string; tables: SchemaTable[] }[]
}
export const fetchSchema = (): Promise<SchemaResponse> => getJson<SchemaResponse>('/api/schema')

// ---------------------------------------------------------------------------
// SQL runner
// ---------------------------------------------------------------------------

export interface SqlRunResponse {
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  elapsed_ms: number
  max_rows: number
  timeout_ms: number
}
export const runSql = (sql: string, maxRows = 1000, signal?: AbortSignal): Promise<SqlRunResponse> =>
  postJson<SqlRunResponse>('/api/sql/run', { sql, max_rows: maxRows }, signal)

// ---------------------------------------------------------------------------
// Assistant capability probe
// ---------------------------------------------------------------------------

export interface AssistantStatus {
  configured: boolean
  model: string
}
export const fetchAssistantStatus = (): Promise<AssistantStatus> =>
  getJson<AssistantStatus>('/api/assistant/status')

export { GamesApiError }
