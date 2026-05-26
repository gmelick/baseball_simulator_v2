/**
 * useGameSocket.ts — SIM-393 (consumes the SIM-385 WS schema)
 *
 * Subscribes to the per-game live WebSocket (`/ws/games/{gamePk}`, proxied by
 * Vite to the backend) and surfaces the latest live state + connection status.
 *
 * Event handling (SIM-385 WsEventType):
 *   - game_state_update → updates `liveState` (+ `resimPending` from resim_triggered)
 *   - resim_pending     → sets `resimPending` true (a new sim is being queued)
 *   - ping              → replies with a pong (keepalive)
 *   - pong              → ignored
 *
 * Reconnects with a fixed backoff on unexpected close. Returns a stable object
 * the Game page reads each render.
 */
import { useEffect, useRef, useState } from 'react'

export type SocketStatus = 'connecting' | 'open' | 'closed'

/** Normalized live state from a WS game_state_update (field names unified with
 *  the REST /live payload: on_1b/on_2b/on_3b). All fields optional — the wire
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
  /** epoch ms of the last message, or null. */
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

/** Subscribe to the live WS for one game. Pass `enabled=false` to not connect
 *  (e.g. for a final game that will never emit). */
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
            // pong / unknown — just record liveness.
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
