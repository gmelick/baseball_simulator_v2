/**
 * ResultGrid.tsx — SIM-439
 * A dense, sortable result table used by BOTH the SQL Console and the AI
 * assistant. Sticky header, monospace right-aligned numerics, muted NULL glyph,
 * click-to-sort, an overflow-x scroller (the page body never scrolls sideways),
 * and player-id cells rendered as deep links to /player/:id.
 */
import React, { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import styles from './ResultGrid.module.css'

export interface ResultGridProps {
  columns: string[]
  rows: unknown[][]
  /** Column names whose integer cells link to the player detail page. */
  idColumns?: string[]
  /** Max rows to render (windowing guard for very wide results). */
  maxRender?: number
}

const DEFAULT_ID_COLS = ['player_id', 'entity_id', 'pitcher', 'batter', 'catcher_id', 'manager_id']

function isNumeric(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

export function ResultGrid({ columns, rows, idColumns = DEFAULT_ID_COLS, maxRender = 500 }: ResultGridProps): React.ReactElement {
  const [sort, setSort] = useState<{ col: number; dir: 1 | -1 } | null>(null)
  const idSet = useMemo(() => new Set(idColumns), [idColumns])

  const sorted = useMemo(() => {
    if (!sort) return rows
    const { col, dir } = sort
    return [...rows].sort((a, b) => {
      const av = a[col]
      const bv = b[col]
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      if (isNumeric(av) && isNumeric(bv)) return (av - bv) * dir
      return String(av).localeCompare(String(bv)) * dir
    })
  }, [rows, sort])

  const shown = sorted.slice(0, maxRender)

  const toggleSort = (col: number): void =>
    setSort((s) => (s && s.col === col ? { col, dir: s.dir === 1 ? -1 : 1 } : { col, dir: -1 }))

  if (columns.length === 0) {
    return <p className={styles.empty}>No columns.</p>
  }

  return (
    <div className={styles.scroller}>
      <table className={styles.grid} role="grid">
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th
                key={c + i}
                scope="col"
                className={styles.th}
                onClick={() => toggleSort(i)}
                aria-sort={sort?.col === i ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}
              >
                {c}
                {sort?.col === i && <span className={styles.sortCaret}>{sort.dir === 1 ? ' ▲' : ' ▼'}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, ri) => (
            <tr key={ri} className={styles.tr}>
              {row.map((cell, ci) => {
                const colName = columns[ci]
                const numeric = isNumeric(cell)
                const cls = `${styles.td} ${numeric ? styles.num : ''}`
                if (cell == null) {
                  return (
                    <td key={ci} className={cls}>
                      <span className={styles.null}>NULL</span>
                    </td>
                  )
                }
                if (idSet.has(colName) && isNumeric(cell)) {
                  return (
                    <td key={ci} className={cls}>
                      <Link className={styles.playerLink} to={`/player/${cell}`}>
                        {cell}
                      </Link>
                    </td>
                  )
                }
                return (
                  <td key={ci} className={cls}>
                    {String(cell)}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {sorted.length > shown.length && (
        <p className={styles.windowNote}>
          Showing the first {shown.length.toLocaleString()} of {sorted.length.toLocaleString()} rows.
        </p>
      )}
    </div>
  )
}
