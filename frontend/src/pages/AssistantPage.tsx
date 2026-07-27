/**
 * AssistantPage.tsx — SIM-439
 * The AI assistant chat. Probes /api/assistant/status on mount; if not
 * configured, renders NotConfiguredState. Otherwise a streaming chat where each
 * assistant turn shows its prose, the exact SQL it ran (Copy / Open in Console),
 * the inline result grid with a transparency line, and any errors.
 */
import React, { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { fetchAssistantStatus } from '@/api/lab'
import { askAssistant, type AssistantHistoryTurn } from '@/api/assistant'
import { ResultGrid } from '@/components/lab/ResultGrid'
import { LoadingBlock, NotConfiguredState } from '@/components/lab/states'
import { Badge } from '@/components/ui'
import { useAsync } from '@/hooks/useAsync'

import styles from './assistant.module.css'

type Block =
  | { kind: 'text'; text: string }
  | { kind: 'query'; sql: string; step: number }
  | { kind: 'result'; columns: string[]; rows: unknown[][]; row_count: number; truncated: boolean; elapsed_ms: number; step: number }
  | { kind: 'error'; detail: string }

interface Turn {
  role: 'user' | 'assistant'
  text?: string
  blocks?: Block[]
}

const SUGGESTIONS = [
  'Top 15 pitchers by whiff rate in 2024 (min 1000 pitches)',
  "Show Gerrit Cole's 2024 arsenal and how his fastball velo trended by month",
  'Which right-handed batters crushed lefties for power in 2024?',
  "Why might a starter's strikeout projection be low? Show me chase and put-away rates.",
]

/** Minimal, safe markdown-ish renderer: **bold**, `code`, newlines. Never uses
 * dangerouslySetInnerHTML. */
function renderText(text: string): React.ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) return <strong key={i}>{p.slice(2, -2)}</strong>
    if (p.startsWith('`') && p.endsWith('`')) return <code key={i}>{p.slice(1, -1)}</code>
    return <React.Fragment key={i}>{p}</React.Fragment>
  })
}

function SqlBlock({ sql, step }: { sql: string; step: number }): React.ReactElement {
  const navigate = useNavigate()
  return (
    <div className={styles.sqlBlock}>
      <div className={styles.sqlHeader}>
        <span>Query {step}</span>
        <div className={styles.sqlActions}>
          <button className={styles.sqlBtn} onClick={() => void navigator.clipboard?.writeText(sql)}>
            Copy
          </button>
          <button className={styles.sqlBtn} onClick={() => navigate('/lab/console', { state: { sql } })}>
            Open in Console
          </button>
        </div>
      </div>
      <pre className={styles.sqlCode}>{sql}</pre>
    </div>
  )
}

export function AssistantPage(): React.ReactElement {
  const status = useAsync(() => fetchAssistantStatus(), [])
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const threadRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight })
  }, [turns])

  const send = (question: string): void => {
    const q = question.trim()
    if (!q || busy) return
    const history: AssistantHistoryTurn[] = turns
      .map((t): AssistantHistoryTurn => ({
        role: t.role,
        content: t.role === 'user' ? (t.text ?? '') : (t.blocks ?? []).filter((b): b is { kind: 'text'; text: string } => b.kind === 'text').map((b) => b.text).join('\n'),
      }))
      .filter((t) => t.content)
    setTurns((t) => [...t, { role: 'user', text: q }, { role: 'assistant', blocks: [] }])
    setInput('')
    setBusy(true)

    void askAssistant(q, history, (ev) => {
      if (ev.type === 'done') {
        setBusy(false)
        return
      }
      setTurns((t) => {
        const copy = [...t]
        const last = copy[copy.length - 1]
        if (!last || last.role !== 'assistant') return t
        const blocks = [...(last.blocks ?? [])]
        if (ev.type === 'text') blocks.push({ kind: 'text', text: ev.text })
        else if (ev.type === 'query') blocks.push({ kind: 'query', sql: ev.sql, step: ev.step })
        else if (ev.type === 'result')
          blocks.push({
            kind: 'result',
            columns: ev.columns,
            rows: ev.rows,
            row_count: ev.row_count,
            truncated: ev.truncated,
            elapsed_ms: ev.elapsed_ms,
            step: ev.step,
          })
        else if (ev.type === 'error') blocks.push({ kind: 'error', detail: ev.detail })
        copy[copy.length - 1] = { role: 'assistant', blocks }
        return copy
      })
    })
  }

  if (status.kind === 'loading') return <div className={styles.page}><LoadingBlock /></div>
  if (status.kind === 'ok' && !status.data.configured) {
    return (
      <div className={styles.page}>
        <NotConfiguredState />
      </div>
    )
  }

  return (
    <div className={styles.page}>
      <div className={styles.headerStrip}>
        <Badge variant="warning">read-only</Badge>
        <span className={styles.headerNote}>
          The assistant writes and runs SELECT-only queries. You can see, edit, and re-run every query it runs.
          {status.kind === 'ok' && ` · ${status.data.model}`}
        </span>
      </div>

      <div className={styles.thread} ref={threadRef} aria-live="polite">
        {turns.length === 0 && (
          <>
            <p className={styles.msgText}>
              Ask a question about the pitch-level data. The assistant will write the SQL, run it read-only, and
              explain the result.
            </p>
            <div className={styles.chips}>
              {SUGGESTIONS.map((s) => (
                <button key={s} className={styles.chip} onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </>
        )}

        {turns.map((t, i) =>
          t.role === 'user' ? (
            <div key={i} className={styles.turnUser}>
              {t.text}
            </div>
          ) : (
            <div key={i} className={styles.turnAssistant}>
              {(t.blocks ?? []).map((b, j) => {
                if (b.kind === 'text') return <div key={j} className={styles.msgText}>{renderText(b.text)}</div>
                if (b.kind === 'query') return <SqlBlock key={j} sql={b.sql} step={b.step} />
                if (b.kind === 'error') return <div key={j} className={styles.errorLine}>{b.detail}</div>
                return (
                  <div key={j}>
                    <ResultGrid columns={b.columns} rows={b.rows} maxRender={100} />
                    <p className={styles.resultLine}>
                      Ran this query · {b.row_count.toLocaleString()} rows · {b.elapsed_ms} ms · read-only
                    </p>
                  </div>
                )
              })}
              {busy && i === turns.length - 1 && (t.blocks ?? []).length === 0 && <span className={styles.caret} />}
            </div>
          ),
        )}
      </div>

      <div className={styles.composer}>
        <textarea
          className={styles.composerInput}
          value={input}
          placeholder="Ask about pitchers, batters, arsenals, counts…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send(input)
            }
          }}
          aria-label="Ask the assistant"
        />
        <button className={styles.sendBtn} onClick={() => send(input)} disabled={busy || !input.trim()}>
          {busy ? '…' : 'Send'}
        </button>
      </div>
    </div>
  )
}
