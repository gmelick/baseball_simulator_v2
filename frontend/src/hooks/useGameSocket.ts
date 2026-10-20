/**
 * useGameSocket.ts — the hook for the per-game live WebSocket.
 *
 * The hook subscribes to `/ws/games/{gamePk}` (the Vite dev server proxies it
 * to the backend). It returns the latest live state and the connection status.
 * The live service builds each event as a plain dict
 * (pipeline/live/live_ingestion_pipeline.py) and publishes it through
 * pipeline/live/broadcast.py. The types in this file are the frontend's
 * description of those events.
 *
 * Event handling:
 *   - game_state_update → updates `liveState` (and `resimPending` from resim_triggered)
 *   - resim_pending     → sets `resimPending` true (a new sim is queued)
 *   - ping              → replies with a pong (keepalive)
 *   - pong              → records liveness only
 *
 * The hook reconnects after a fixed delay on an unexpected close. The Game
 * page reads the returned state each render.
 */
import { useEffect, useRef, useState } from 'react'

export type SocketStatus = 'connecting' | 'open' | 'closed'

/** The live state from a WS game_state_update. The runner fields use the REST
 *  /live names (on_1b/on_2b/on_3b). Every field is optional, because the wire
 *  state can be partial. */
export interface WsLiveState {
  inning?: number | null
  half?: string | null
  outs?: number | null
  balls?: number | null
  strikes?: number | null
  home_score?: number | null
  away_score?: number | null
  on_1b?: number | null
  on_2b?: number | null
  on_3b?: number | null
  current_batter_id?: number | null
  current_pitcher_id?: number | null
  game_status?: string | null
}

export interface GameSocketState {
  status: SocketStatus
  liveState: WsLiveState | null
  resimPending: boolean
  /** The epoch ms of the last message, or null. */
  lastEventAt: number | null
}

interface WsGameStatePayload {
  inning?: number | null
  half?: string | null
  outs?: number | null
  balls?: number | null
  strikes?: number | null
  home_score?: number | null
  away_score?: number | null
  runner_on_first?: number | null
  runner_on_second?: number | null
  runner_on_third?: number | null
  current_batter_id?: number | null
  current_pitcher_id?: number | null
  game_status?: string | null
}

function mapWsState(gs: WsGameStatePayload): WsLiveState {
  return {
    inning: gs.inning,
    half: gs.half,
    outs: gs.outs,
    balls: gs.balls,
    strikes: gs.strikes,
    home_score: gs.home_score,
    away_score: gs.away_score,
    on_1b: gs.runner_on_first,
    on_2b: gs.runner_on_second,
    on_3b: gs.runner_on_third,
    current_batter_id: gs.current_batter_id,
    current_pitcher_id: gs.current_pitcher_id,
    game_status: gs.game_status,
  }
}

const RECONNECT_MS = 3000

/** Subscribe to the live WS for one game. Pass `enabled=false` to skip the
 *  connection (for example, a final game that sends nothing). */
export function useGameSocket(gamePk: number, enabled = true): GameSocketState {
  const [state, setState] = useState<GameSocketState>({
    status: 'closed',
    liveState: null,
    resimPending: false,
    lastEventAt: null,
  })

  const socketRef = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const closedByUs = useRef(false)

  useEffect(() => {
    if (!enabled) {
      setState((s) => ({ ...s, status: 'closed' }))
      return
    }

    closedByUs.current = false

    const connect = (): void => {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const url = `${proto}//${window.location.host}/ws/games/${gamePk}`
      setState((s) => ({ ...s, status: 'connecting' }))

      let ws: WebSocket
      try {
        ws = new WebSocket(url)
      } catch {
        scheduleReconnect()
        return
      }
      socketRef.current = ws

      ws.onopen = () => setState((s) => ({ ...s, status: 'open' }))

      ws.onmessage = (ev: MessageEvent<string>) => {
        let msg: { type?: string; game_state?: WsGameStatePayload; resim_triggered?: boolean }
        try {
          msg = JSON.parse(ev.data)
        } catch {
          return
        }
        const now = Date.now()
        switch (msg.type) {
          case 'game_state_update':
            setState((s) => ({
              ...s,
              liveState: msg.game_state ? mapWsState(msg.game_state) : s.liveState,
              resimPending: msg.resim_triggered ?? s.resimPending,
              lastEventAt: now,
            }))
            break
          case 'resim_pending':
            setState((s) => ({ ...s, resimPending: true, lastEventAt: now }))
            break
          case 'ping':
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'pong' }))
            }
            setState((s) => ({ ...s, lastEventAt: now }))
            break
          default:
            // A pong or an unknown type: record liveness only.
            setState((s) => ({ ...s, lastEventAt: now }))
        }
      }

      ws.onclose = () => {
        setState((s) => ({ ...s, status: 'closed' }))
        if (!closedByUs.current) scheduleReconnect()
      }

      ws.onerror = () => ws.close()
    }

    const scheduleReconnect = (): void => {
      if (closedByUs.current) return
      reconnectRef.current = setTimeout(connect, RECONNECT_MS)
    }

    connect()

    return () => {
      closedByUs.current = true
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [gamePk, enabled])

  return state
}
