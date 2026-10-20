/**
 * lab.ts — the browser's client for the Data Lab: the summary analytics, the
 * data freshness, the schema tree, the read-only SQL runner and the
 * AI-assistant capability probe.
 *
 * The requests go through the shared helpers in http.ts. The response types mirror the FastAPI models in
 * api/routes/{analytics,data_health,schema_introspect,sql_runner,ai_assistant}.py
 * by hand. Keep them in sync.
 */
import { getJson, postJson } from '@/api/http'



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

/**
 * The games a season read covers (the Data Lab game-type filter, SIM-572).
 * 'R' (the API's default) reads the regular season alone; 'all' adds the
 * postseason.
 */
export type GameType = 'R' | 'all'

export interface AnalyticsFilter {
  season: number
  pitcher?: number | null
  batter?: number | null
  stand?: string | null
  pThrows?: string | null
  gameType?: GameType
}

export const fetchKpis = (): Promise<Kpis> => getJson<Kpis>('/api/analytics/kpis')
export const fetchBySeason = (): Promise<SeasonRow[]> => getJson<SeasonRow[]>('/api/analytics/by-season')

export const fetchPitchTypeMix = (f: AnalyticsFilter): Promise<PitchTypeRow[]> =>
  getJson<PitchTypeRow[]>(
    `/api/analytics/pitch-type-mix${qs({ season: f.season, pitcher: f.pitcher, stand: f.stand, game_type: f.gameType })}`,
  )

export const fetchPaOutcomes = (f: AnalyticsFilter): Promise<OutcomeRow[]> =>
  getJson<OutcomeRow[]>(
    `/api/analytics/pa-outcomes${qs({ season: f.season, batter: f.batter, p_throws: f.pThrows, game_type: f.gameType })}`,
  )

export const fetchCountState = (f: AnalyticsFilter): Promise<CountStateRow[]> =>
  getJson<CountStateRow[]>(
    `/api/analytics/count-state${qs({ season: f.season, pitcher: f.pitcher, batter: f.batter, game_type: f.gameType })}`,
  )

export const fetchVeloDistribution = (f: AnalyticsFilter & { pitchType?: string }): Promise<VeloBucketRow[]> =>
  getJson<VeloBucketRow[]>(
    `/api/analytics/velo-distribution${qs({ season: f.season, pitcher: f.pitcher, pitch_type: f.pitchType, game_type: f.gameType })}`,
  )

export const fetchZone = (f: AnalyticsFilter): Promise<ZoneRow[]> =>
  getJson<ZoneRow[]>(`/api/analytics/zone${qs({ season: f.season, pitcher: f.pitcher, game_type: f.gameType })}`)

// ---------------------------------------------------------------------------
// Data freshness
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
  postJson<SqlRunResponse>('/api/sql/run', { sql, max_rows: maxRows }, { signal })

// ---------------------------------------------------------------------------
// Assistant capability probe
// ---------------------------------------------------------------------------

export interface AssistantStatus {
  configured: boolean
  model: string
}
export const fetchAssistantStatus = (): Promise<AssistantStatus> =>
  getJson<AssistantStatus>('/api/assistant/status')

