/**
 * assistant.ts — SIM-439
 * SSE streaming client for the AI assistant (api/routes/ai_assistant.py).
 * The POST body streams newline-delimited `data: {json}` frames; this parser
 * turns them into typed AssistantEvents delivered via an onEvent callback.
 */

export interface AssistantResultEvent {
  type: 'result'
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  elapsed_ms: number
  step: number
}

export type AssistantEvent =
  | { type: 'text'; text: string; step: number }
  | { type: 'query'; sql: string; step: number }
  | AssistantResultEvent
  | { type: 'error'; detail: string; step?: number }
  | { type: 'done' }

export interface AssistantHistoryTurn {
  role: 'user' | 'assistant'
  content: string
}

/**
 * Stream an assistant answer. Resolves when the stream ends; each parsed frame
 * is delivered to onEvent. A non-2xx response (e.g. 503 not-configured) is
 * surfaced as an {type:'error'} + {type:'done'} pair.
 */
export async function askAssistant(
  question: string,
  history: AssistantHistoryTurn[],
  onEvent: (ev: AssistantEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response
  try {
    res = await fetch('/api/assistant/ask', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, history }),
      signal,
    })
  } catch (err) {
    if ((err as Error).name === 'AbortError') return
    onEvent({ type: 'error', detail: 'Network error contacting the assistant.' })
    onEvent({ type: 'done' })
    return
  }

  if (!res.ok || !res.body) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string }
    onEvent({ type: 'error', detail: body.detail ?? `Assistant request failed (${res.status}).` })
    onEvent({ type: 'done' })
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const line = frame.split('\n').find((l) => l.startsWith('data:'))
        if (!line) continue
        const jsonStr = line.slice(line.indexOf(':') + 1).trim()
        if (!jsonStr) continue
        try {
          onEvent(JSON.parse(jsonStr) as AssistantEvent)
        } catch {
          /* ignore a malformed frame */
        }
      }
    }
  } catch (err) {
    if ((err as Error).name !== 'AbortError') {
      onEvent({ type: 'error', detail: 'Assistant stream interrupted.' })
      onEvent({ type: 'done' })
    }
  }
}
