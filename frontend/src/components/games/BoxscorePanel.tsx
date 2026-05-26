/**
 * BoxscorePanel.tsx — SIM-394
 *
 * Per-player projections: the 100-iteration boxscore means (SIM-366) plus, on
 * demand, the full prop distribution (SIM-390) with a prop-line marker.
 *
 * The boxscore endpoint runs an N-iteration sim, so it's gated behind an
 * explicit "Load projections" button rather than firing on mount. Once loaded,
 * each player's prop means render as chips; clicking a chip fetches that prop's
 * PMF and shows the distribution chart, with an optional line input that
 * redraws the over/under marker.
 */
import React, { useEffect, useState } from 'react'

import {
  fetchBoxscore,
  fetchPropEdge,
  type BoxscoreCard,
  type PropEdge,
} from '@/api/games'

import { PropDistributionChart } from './PropDistributionChart'
import styles from './BoxscorePanel.module.css'

export interface BoxscorePanelProps {
  gamePk: number
}

interface Selection {
  playerId: number
  prop: string
}

export function BoxscorePanel({ gamePk }: BoxscorePanelProps): React.ReactElement {
  const [box, setBox] = useState<BoxscoreCard | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [selection, setSelection] = useState<Selection | null>(null)
  const [lineInput, setLineInput] = useState('')
  const [dist, setDist] = useState<PropEdge | null>(null)
  const [distLoading, setDistLoading] = useState(false)

  const loadBoxscore = (): void => {
    setLoading(true)
    setError(null)
    fetchBoxscore(gamePk, 100)
      .then((b) => setBox(b))
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : 'Failed to load projections.'),
      )
      .finally(() => setLoading(false))
  }

  // Fetch the selected prop's distribution (re-runs when the line changes).
  useEffect(() => {
    if (!selection) {
      setDist(null)
      return
    }
    let cancelled = false
    setDistLoading(true)
    const lineNum = lineInput.trim() === '' ? undefined : Number(lineInput)
    const q = lineNum != null && Number.isFinite(lineNum) ? { line: lineNum } : {}
    fetchPropEdge(gamePk, selection.playerId, selection.prop, q)
      .then((d) => !cancelled && setDist(d))
      .catch(() => !cancelled && setDist(null))
      .finally(() => !cancelled && setDistLoading(false))
    return () => {
      cancelled = true
    }
  }, [gamePk, selection, lineInput])

  if (!box) {
    return (
      <div className={styles.gate}>
        <button type="button" className={styles.loadButton} onClick={loadBoxscore} disabled={loading}>
          {loading ? 'Running 100-iteration sim…' : 'Load projections'}
        </button>
        {error && <p className={styles.error} role="alert">{error}</p>}
      </div>
    )
  }

  const players = Object.values(box.players).sort((a, b) => a.player_id - b.player_id)

  return (
    <div className={styles.panel}>
      <p className={styles.caption}>{box.n_iterations}-iteration averages. Click a prop to see its distribution.</p>

      <ul className={styles.playerList}>
        {players.map((row) => (
          <li key={row.player_id} className={styles.playerRow}>
            <span className={styles.playerId}>Player {row.player_id}</span>
            <span className={styles.chips}>
              {Object.entries(row.means).map(([prop, mean]) => {
                const active =
                  selection?.playerId === row.player_id && selection?.prop === prop
                return (
                  <button
                    key={prop}
                    type="button"
                    className={`${styles.chip} ${active ? styles.chipActive : ''}`}
                    onClick={() =>
                      setSelection(active ? null : { playerId: row.player_id, prop })
                    }
                    aria-pressed={active}
                  >
                    <span className={styles.chipProp}>{prop}</span>
                    <span className={styles.chipMean}>{mean.toFixed(2)}</span>
                  </button>
                )
              })}
            </span>
          </li>
        ))}
      </ul>

      {selection && (
        <div className={styles.detail}>
          <div className={styles.detailHeader}>
            <strong>
              Player {selection.playerId} — {selection.prop}
            </strong>
            <label className={styles.lineLabel}>
              Line
              <input
                type="number"
                step="0.5"
                className={styles.lineInput}
                value={lineInput}
                onChange={(e) => setLineInput(e.target.value)}
                placeholder="e.g. 5.5"
              />
            </label>
          </div>

          {distLoading && <p className={styles.muted}>Loading distribution…</p>}

          {!distLoading && dist && (
            <>
              <PropDistributionChart
                support={dist.support}
                probabilities={dist.probabilities}
                line={dist.line}
                mean={dist.mean}
                propLabel={dist.prop}
              />
              <div className={styles.stats}>
                <span>mean {dist.mean.toFixed(2)}</span>
                <span>median {dist.median.toFixed(1)}</span>
                <span>sd {dist.std.toFixed(2)}</span>
                {dist.p_over != null && <span>P(over) {(dist.p_over * 100).toFixed(0)}%</span>}
                {dist.p_under != null && <span>P(under) {(dist.p_under * 100).toFixed(0)}%</span>}
                {dist.p_push != null && dist.p_push > 0 && (
                  <span>P(push) {(dist.p_push * 100).toFixed(0)}%</span>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
