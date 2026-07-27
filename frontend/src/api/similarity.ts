/**
 * similarity.ts — SIM-439
 * Client for the generalized Similarity Explorer (api/routes/similarity_explorer.py).
 * Handles the 8 score engines (comps + component sub-scores) and the situation
 * (distance) engine. 503 → engine warming; 404 → subject not found / distance
 * engine on a score path.
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
async function postJson<T>(url: string, payload: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<T>
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export interface EngineCatalogEntry {
  engine: string
  kind: 'score' | 'distance'
  method: string
  partition: string | null
  sub_score_count: number
  built: boolean
  profile_count: number
  note: string
}

export interface SubScoreMeta {
  field: string
  label: string
  weight: number | null
}

export interface EngineMeta {
  engine: string
  method: string
  partition: string
  sub_scores: SubScoreMeta[]
  sample_field: string
  min_sample: number
  supports_vs_hand: boolean
  position_keyed: boolean
  built: boolean
  profile_count: number
  note: string
}

export interface SimMember {
  entity_id: number
  season: number
  name: string
  score: number
  sub_scores: Record<string, number>
  sample: number
  below_min_sample: boolean
  extra: Record<string, unknown>
}

export interface SimBin {
  bin_index: number
  lo: number
  hi: number
  count: number
  preview: SimMember[]
  members: SimMember[]
}

export interface SimQueryResponse {
  engine: string
  method: string
  subject: Record<string, unknown>
  sub_scores_meta: SubScoreMeta[]
  sample_field: string
  min_sample: number
  partition: string
  population_size: number
  score_summary: Record<string, number>
  diagnostic: Record<string, unknown>
  bins: SimBin[]
  top_n: SimMember[]
  note: string
}

export interface SimPairResponse {
  engine: string
  subject: Record<string, unknown>
  comp: SimMember
  sub_scores_meta: SubScoreMeta[]
  note: string
}

export interface NearestSituation {
  play_id: string
  game_pk: number
  distance: number
  inning: number
  outs: number
  runners: number
  leverage_index: number
  score_differential: number
}
export interface SituationQueryResponse {
  k: number
  count: number
  results: NearestSituation[]
}

export interface SimQueryParams {
  entityId: number
  season: number
  position?: string
  vsHand?: string
  bins?: number
  topN?: number
}

export const fetchEngineCatalog = (): Promise<EngineCatalogEntry[]> =>
  getJson<EngineCatalogEntry[]>('/api/similarity/engines')

export const fetchEngineMeta = (engine: string, position?: string, vsHand?: string): Promise<EngineMeta> =>
  getJson<EngineMeta>(`/api/similarity/${engine}/meta${qs({ position, vs_hand: vsHand })}`)

export const fetchSimilarity = (engine: string, p: SimQueryParams): Promise<SimQueryResponse> =>
  getJson<SimQueryResponse>(
    `/api/similarity/${engine}/query${qs({
      entity_id: p.entityId,
      season: p.season,
      position: p.position,
      vs_hand: p.vsHand,
      bins: p.bins,
      top_n: p.topN,
    })}`,
  )

export const fetchSimilarityPair = (
  engine: string,
  a: { id: number; season: number },
  b: { id: number; season: number },
  opts: { position?: string; vsHand?: string } = {},
): Promise<SimPairResponse> =>
  getJson<SimPairResponse>(
    `/api/similarity/${engine}/pair${qs({
      a_id: a.id,
      a_season: a.season,
      b_id: b.id,
      b_season: b.season,
      position: opts.position,
      vs_hand: opts.vsHand,
    })}`,
  )

export interface SituationVectorInput {
  inning: number
  top_or_bottom: number
  outs: number
  runner_on_1b: number
  runner_on_2b: number
  runner_on_3b: number
  score_differential: number
  leverage_index: number
  pitcher_pitch_count: number
  batter_pa_count: number
  park_factor_runs: number
  k: number
}
export const fetchSituation = (vec: SituationVectorInput): Promise<SituationQueryResponse> =>
  postJson<SituationQueryResponse>('/api/similarity/situation/query', vec)
