/**
 * useAsync.ts — SIM-439
 * A tiny data-fetch hook: runs `fn(signal)` whenever `deps` change, with an
 * AbortController + cancelled guard (matching the DaySummaryPage pattern) and a
 * discriminated-union state so each caller renders loading / ok / error inline.
 */
import { useEffect, useRef, useState } from 'react'

import { GamesApiError } from '@/api/games'

export type AsyncState<T> =
  | { kind: 'loading' }
  | { kind: 'ok'; data: T }
  | { kind: 'error'; message: string; status?: number }

export function useAsync<T>(fn: (signal: AbortSignal) => Promise<T>, deps: unknown[]): AsyncState<T> {
  const fnRef = useRef(fn)
  fnRef.current = fn
  const [state, setState] = useState<AsyncState<T>>({ kind: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    setState({ kind: 'loading' })
    fnRef
      .current(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ kind: 'ok', data })
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || (err as Error)?.name === 'AbortError') return
        const status = err instanceof GamesApiError ? err.status : undefined
        const message = err instanceof Error ? err.message : 'Request failed.'
        setState({ kind: 'error', message, status })
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return state
}
