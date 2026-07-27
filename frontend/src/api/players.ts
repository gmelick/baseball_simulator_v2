/**
 * players.ts — SIM-439
 * Name typeahead + player identity (api/routes/players.py). Reused by the
 * universal PlayerSearchInput and the Player Detail hub.
 */
import { GamesApiError } from '@/api/games'

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { credentials: 'include', signal })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new GamesApiError(res.status, body.detail ?? `Request failed (${res.status}).`)
  }
  return res.json() as Promise<T>
}

export interface PlayerHit {
  player_id: number
  name: string
  bats: string | null
  throws: string | null
  position: string | null
}

export interface PlayerDetail extends PlayerHit {
  seasons: number[]
}

export const searchPlayers = (q: string, limit = 20, signal?: AbortSignal): Promise<PlayerHit[]> =>
  getJson<PlayerHit[]>(`/api/players/search?q=${encodeURIComponent(q)}&limit=${limit}`, signal)

export const fetchPlayer = (playerId: number): Promise<PlayerDetail> =>
  getJson<PlayerDetail>(`/api/players/${playerId}`)
