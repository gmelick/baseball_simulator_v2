/**
 * OverrideDeltaView.tsx — SIM-397 (shared by SIM-398)
 *
 * Renders a baseline-vs-override comparison table from a WithOverrideResponse
 * delta: one row per metric (win%, score means, …) with baseline, override, and
 * the signed delta (green up / red down). A small fractional metric like a
 * win-pct is shown as a percentage; everything else as a 2-dp number.
 */
import React from 'react'

import type { OverrideDelta } from '@/api/games'

import styles from './OverrideDeltaView.module.css'

export interface OverrideDeltaViewProps {
  delta: OverrideDelta
}

const PCT_METRICS = new Set(['home_win_pct', 'away_win_pct', 'tie_pct'])

function fmt(metric: string, v: number): string {
  if (PCT_METRICS.has(metric)) return `${(v * 100).toFixed(1)}%`
  return v.toFixed(2)
}

function fmtDelta(metric: string, v: number): string {
  const s = PCT_METRICS.has(metric) ? `${(v * 100).toFixed(1)}%` : v.toFixed(2)
  return v > 0 ? `+${s}` : s
}

function prettyMetric(m: string): string {
  return m.replace(/_/g, ' ')
}

export function OverrideDeltaView({ delta }: OverrideDeltaViewProps): React.ReactElement {
  const rows = Object.values(delta.metrics)
  if (rows.length === 0) {
    return <p className={styles.empty}>No metric deltas returned.</p>
  }

  return (
    <div className={styles.wrap}>
      {delta.description && <p className={styles.desc}>{delta.description}</p>}
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col" className={styles.metricCol}>Metric</th>
            <th scope="col">Baseline</th>
            <th scope="col">Override</th>
            <th scope="col">Δ</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const cls =
              Math.abs(r.delta) < 1e-9
                ? styles.deltaFlat
                : r.delta > 0
                  ? styles.deltaPos
                  : styles.deltaNeg
            return (
              <tr key={r.metric}>
                <th scope="row" className={styles.metricCol}>{prettyMetric(r.metric)}</th>
                <td>{fmt(r.metric, r.baseline)}</td>
                <td>{fmt(r.metric, r.override)}</td>
                <td className={cls}>{fmtDelta(r.metric, r.delta)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
