/**
 * players.ts — the name typeahead and the player identity client
 * (api/routes/players.py). The universal PlayerSearchInput and the Player
 * Detail hub use it.
 */
import { getJson } from '@/api/http'


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
