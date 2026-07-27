/**
 * SituationFinderPage.tsx — SIM-439
 * The honest home for the KDTree situation engine: it compares GAME STATES, not
 * players, and returns a DISTANCE (lower = more similar), not a 0–1 score. A
 * vector-builder form on the left, the nearest historical situations on the
 * right. No composite / sub-score UI (there is none to fake).
 */
import React, { useState } from 'react'
import { Link } from 'react-router-dom'

import { fetchSituation, type SituationVectorInput } from '@/api/similarity'
import { EngineWarmingState, ErrorBlock, LoadingBlock } from '@/components/lab/states'
import { useAsync } from '@/hooks/useAsync'

import styles from './similarity.module.css'

const DEFAULT_VEC: SituationVectorInput = {
  inning: 9,
  top_or_bottom: 1,
  outs: 2,
  runner_on_1b: 1,
  runner_on_2b: 1,
  runner_on_3b: 0,
  score_differential: -1,
  leverage_index: 3.2,
  pitcher_pitch_count: 95,
  batter_pa_count: 4,
  park_factor_runs: 1.0,
  k: 25,
}

function runnersLabel(mask: number): string {
  const b: string[] = []
  if (mask & 1) b.push('1B')
  if (mask & 2) b.push('2B')
  if (mask & 4) b.push('3B')
  return b.length ? b.join(' ') : 'empty'
}

function SituationResults({ vec }: { vec: SituationVectorInput }): React.ReactElement {
  const q = useAsync(() => fetchSituation(vec), [JSON.stringify(vec)])
  if (q.kind === 'loading') return <LoadingBlock label="Searching game states…" />
  if (q.kind === 'error') {
    if (q.status === 503) return <EngineWarmingState engine="situation" />
    return <ErrorBlock message={q.message} />
  }
  if (q.data.count === 0) return <p className={styles.popNote}>No neighbors found.</p>
  return (
    <div className={styles.panel}>
      <p className={styles.distanceBanner}>Lower distance = more similar. This is a distance, not a 0–1 score.</p>
      <div style={{ overflowX: 'auto' }}>
        <table className={styles.select} style={{ width: '100%', borderCollapse: 'collapse', border: 'none' }}>
          <thead>
            <tr>
              {['#', 'Distance', 'Game', 'Inning', 'Outs', 'Runners', 'Leverage', 'Score diff'].map((h) => (
                <th key={h} style={{ textAlign: 'left', padding: '6px 10px', borderBottom: '1px solid var(--sim-color-border-strong)', fontSize: 'var(--sim-text-xs)' }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody style={{ fontFamily: 'var(--sim-font-mono)', fontSize: 'var(--sim-text-xs)' }}>
            {q.data.results.map((r, i) => (
              <tr key={r.play_id}>
                <td style={{ padding: '4px 10px' }}>{i + 1}</td>
                <td style={{ padding: '4px 10px' }}>{r.distance.toFixed(3)}</td>
                <td style={{ padding: '4px 10px' }}>{r.game_pk}</td>
                <td style={{ padding: '4px 10px' }}>{r.inning}</td>
                <td style={{ padding: '4px 10px' }}>{r.outs}</td>
                <td style={{ padding: '4px 10px' }}>{runnersLabel(r.runners)}</td>
                <td style={{ padding: '4px 10px' }}>{r.leverage_index.toFixed(2)}</td>
                <td style={{ padding: '4px 10px' }}>{r.score_differential > 0 ? `+${r.score_differential}` : r.score_differential}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function SituationFinderPage(): React.ReactElement {
  const [vec, setVec] = useState<SituationVectorInput>(DEFAULT_VEC)
  const [submitted, setSubmitted] = useState<SituationVectorInput | null>(null)

  const num = (key: keyof SituationVectorInput, label: string, step = 1): React.ReactElement => (
    <div className={styles.formRow}>
      <label htmlFor={key}>{label}</label>
      <input
        id={key}
        className={styles.numInput}
        type="number"
        step={step}
        value={vec[key]}
        onChange={(e) => setVec((v) => ({ ...v, [key]: Number(e.target.value) }))}
      />
    </div>
  )
  const bool = (key: keyof SituationVectorInput, label: string): React.ReactElement => (
    <div className={styles.formRow}>
      <label htmlFor={key}>{label}</label>
      <input id={key} type="checkbox" checked={vec[key] === 1} onChange={(e) => setVec((v) => ({ ...v, [key]: e.target.checked ? 1 : 0 }))} />
    </div>
  )

  return (
    <div className={styles.page}>
      <Link className={styles.backLink} to="/similarity">
        ← All engines
      </Link>
      <header className={styles.header}>
        <h1 className={styles.title}>Situation Finder</h1>
        <p className={styles.subtitle}>
          Find the historical game states most similar to a query state — the KDTree situation engine. Useful for
          "what usually happens here". Returns a distance, not a player comp.
        </p>
      </header>

      <div className={styles.sitLayout}>
        <div className={styles.panel}>
          {num('inning', 'Inning')}
          <div className={styles.formRow}>
            <label htmlFor="half">Half</label>
            <select id="half" className={styles.select} value={vec.top_or_bottom} onChange={(e) => setVec((v) => ({ ...v, top_or_bottom: Number(e.target.value) }))}>
              <option value={0}>Top</option>
              <option value={1}>Bottom</option>
            </select>
          </div>
          {num('outs', 'Outs')}
          {bool('runner_on_1b', 'Runner on 1B')}
          {bool('runner_on_2b', 'Runner on 2B')}
          {bool('runner_on_3b', 'Runner on 3B')}
          {num('score_differential', 'Score diff (home−away)')}
          {num('leverage_index', 'Leverage index', 0.1)}
          {num('pitcher_pitch_count', 'Pitcher pitch count')}
          {num('batter_pa_count', 'Batter PA #')}
          {num('park_factor_runs', 'Park factor (runs)', 0.01)}
          {num('k', 'Neighbors (k)')}
          <button className={styles.runBtn} onClick={() => setSubmitted({ ...vec })}>
            Find similar situations
          </button>
        </div>
        <div>
          {submitted ? (
            <SituationResults vec={submitted} />
          ) : (
            <div className={styles.panel}>
              <p className={styles.popNote}>Build a game state on the left and press “Find”.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
