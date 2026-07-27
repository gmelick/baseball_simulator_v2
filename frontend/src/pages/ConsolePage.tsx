/**
 * ConsolePage.tsx — SIM-439
 * The read-only SQL console. A monospace editor (line-number gutter, Tab→2
 * spaces, Ctrl/Cmd+Enter run, Esc cancel) + starter-query menu + a schema
 * browser, over a result grid with CSV export and a localStorage query history.
 * All execution goes through the safe /api/sql/run path.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'

import { fetchSchema, runSql, type SqlRunResponse } from '@/api/lab'
import { downloadCsv } from '@/components/lab/csv'
import { ResultGrid } from '@/components/lab/ResultGrid'
import { STARTER_QUERIES } from '@/components/lab/starterQueries'
import { EmptyState, ErrorBlock, LoadingBlock } from '@/components/lab/states'
import { Badge } from '@/components/ui'
import { useAsync } from '@/hooks/useAsync'

import styles from './console.module.css'

const HISTORY_KEY = 'datalab.sqlHistory'
const DEFAULT_SQL = STARTER_QUERIES[0].sql

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    return raw ? (JSON.parse(raw) as string[]) : []
  } catch {
    return []
  }
}

export function ConsolePage(): React.ReactElement {
  const location = useLocation()
  const prefill = (location.state as { sql?: string } | null)?.sql
  const [sql, setSql] = useState<string>(prefill ?? DEFAULT_SQL)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<SqlRunResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [history, setHistory] = useState<string[]>(loadHistory)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const gutterRef = useRef<HTMLPreElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const schema = useAsync(() => fetchSchema(), [])

  // A card handed us SQL via router state — load it once.
  useEffect(() => {
    if (prefill) setSql(prefill)
  }, [prefill])

  const run = useCallback(async () => {
    if (!sql.trim() || running) return
    const controller = new AbortController()
    abortRef.current = controller
    setRunning(true)
    setError(null)
    try {
      const res = await runSql(sql, 1000, controller.signal)
      setResult(res)
      setHistory((h) => {
        const next = [sql, ...h.filter((q) => q !== sql)].slice(0, 25)
        localStorage.setItem(HISTORY_KEY, JSON.stringify(next))
        return next
      })
    } catch (err) {
      if ((err as Error).name === 'AbortError') setError('Query cancelled.')
      else setError((err as Error).message)
      setResult(null)
    } finally {
      setRunning(false)
      abortRef.current = null
    }
  }, [sql, running])

  const cancel = (): void => abortRef.current?.abort()

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault()
      void run()
    } else if (e.key === 'Escape' && running) {
      cancel()
    } else if (e.key === 'Tab') {
      e.preventDefault()
      const ta = e.currentTarget
      const start = ta.selectionStart
      const end = ta.selectionEnd
      const next = sql.slice(0, start) + '  ' + sql.slice(end)
      setSql(next)
      requestAnimationFrame(() => {
        ta.selectionStart = ta.selectionEnd = start + 2
      })
    }
  }

  const insertAtCursor = (text: string): void => {
    const ta = taRef.current
    if (!ta) {
      setSql((s) => s + text)
      return
    }
    const start = ta.selectionStart
    const end = ta.selectionEnd
    const next = sql.slice(0, start) + text + sql.slice(end)
    setSql(next)
    requestAnimationFrame(() => {
      ta.focus()
      ta.selectionStart = ta.selectionEnd = start + text.length
    })
  }

  const lineCount = sql.split('\n').length
  const gutter = Array.from({ length: lineCount }, (_, i) => i + 1).join('\n')

  return (
    <div className={styles.wide}>
      <div className={styles.layout}>
        <div className={styles.main}>
          <div className={styles.editorPanel}>
            <div className={styles.toolbar}>
              <div className={styles.toolbarLeft}>
                {running ? (
                  <button className={styles.cancelBtn} onClick={cancel}>
                    Cancel
                  </button>
                ) : (
                  <button className={styles.runBtn} onClick={() => void run()} disabled={!sql.trim()}>
                    ▶ Run <span className={styles.helper}>⌘/Ctrl+↵</span>
                  </button>
                )}
                <select
                  className={styles.select}
                  value=""
                  onChange={(e) => {
                    const q = STARTER_QUERIES.find((x) => x.name === e.target.value)
                    if (q) setSql(q.sql)
                  }}
                  aria-label="Starter queries"
                >
                  <option value="">Starter queries…</option>
                  {STARTER_QUERIES.map((q) => (
                    <option key={q.name} value={q.name}>
                      {q.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className={styles.toolbarRight}>
                <Badge variant="warning">read-only</Badge>
              </div>
            </div>
            <div className={styles.editorArea}>
              <pre className={styles.gutter} ref={gutterRef} aria-hidden="true">
                {gutter}
              </pre>
              <textarea
                ref={taRef}
                className={styles.textarea}
                value={sql}
                spellCheck={false}
                onChange={(e) => setSql(e.target.value)}
                onKeyDown={onKeyDown}
                onScroll={(e) => {
                  if (gutterRef.current) gutterRef.current.scrollTop = e.currentTarget.scrollTop
                }}
                aria-label="SQL editor"
              />
            </div>
            <div className={styles.toolbar}>
              <span className={styles.helper}>
                SELECT-only · runs in a read-only transaction with a 5s timeout and a 1,000-row cap. raw.* only
                (derived.*/sim.* live in DuckDB).
              </span>
            </div>
          </div>

          <div className={styles.resultPanel}>
            {running && <LoadingBlock label="Running query…" />}
            {!running && error && (
              <div className={styles.errorPanel} role="alert">
                {error}
              </div>
            )}
            {!running && !error && result && (
              <>
                <div className={styles.resultToolbar}>
                  <div className={styles.resultMeta}>
                    <span>{result.row_count.toLocaleString()} rows</span>
                    <span>{result.elapsed_ms} ms</span>
                    {result.truncated && <span className={styles.truncBadge}>truncated at {result.max_rows}</span>}
                  </div>
                  <div className={styles.toolbarRight}>
                    <button className={styles.iconBtn} onClick={() => downloadCsv(result.columns, result.rows)}>
                      Export CSV
                    </button>
                  </div>
                </div>
                {result.row_count === 0 ? (
                  <EmptyState title="Query returned 0 rows" hint="The query succeeded but matched nothing." />
                ) : (
                  <ResultGrid columns={result.columns} rows={result.rows} />
                )}
              </>
            )}
            {!running && !error && !result && (
              <EmptyState title="Write a SELECT and press ⌘/Ctrl + Enter" hint="Pick a starter query above to get going." />
            )}
          </div>
        </div>

        <aside className={styles.sidebar}>
          <div className={styles.sideCard}>
            <div className={styles.sideHead}>Schema · raw.*</div>
            <div className={styles.sideBody}>
              {schema.kind === 'loading' && <LoadingBlock label="…" />}
              {schema.kind === 'error' && <ErrorBlock message={schema.message} />}
              {schema.kind === 'ok' &&
                schema.data.schemas.flatMap((s) =>
                  s.tables.map((t) => (
                    <div key={t.name} className={styles.tableItem}>
                      <span className={styles.tableName} onClick={() => insertAtCursor(`raw.${t.name}`)} title="insert table name">
                        raw.{t.name}
                      </span>
                      <div>
                        {t.columns.map((c) => (
                          <span
                            key={c.name}
                            className={styles.colChip}
                            title={`${c.data_type} — click to insert`}
                            onClick={() => insertAtCursor(c.name)}
                          >
                            {c.name}
                          </span>
                        ))}
                      </div>
                    </div>
                  )),
                )}
            </div>
          </div>

          <div className={styles.sideCard}>
            <div className={styles.sideHead}>
              History
              {history.length > 0 && (
                <button
                  className={styles.iconBtn}
                  onClick={() => {
                    setHistory([])
                    localStorage.removeItem(HISTORY_KEY)
                  }}
                >
                  clear
                </button>
              )}
            </div>
            <div className={styles.sideBody}>
              {history.length === 0 && <span className={styles.helper}>Your run queries appear here.</span>}
              {history.map((q, i) => (
                <div key={i} className={styles.historyItem} title={q} onClick={() => setSql(q)}>
                  {q.replace(/\s+/g, ' ').slice(0, 60)}
                </div>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </div>
  )
}
