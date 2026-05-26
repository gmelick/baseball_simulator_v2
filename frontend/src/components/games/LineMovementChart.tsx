/**
 * LineMovementChart.tsx — SIM-396
 *
 * A small time-series chart of one side's no-vig implied probability from open
 * to close (`implied_prob_series`), with a marker on the closing quote and
 * sharp/steam/CLV badges. Pure SVG, auto-scaled Y to the series range.
 */
import React from 'react'

import type { LineMovement } from '@/api/betting'
import { Badge } from '@/components/ui'

import styles from './LineMovementChart.module.css'

export interface LineMovementChartProps {
  movement: LineMovement
}

const VB_W = 320
const VB_H = 140
const PAD = 18

export function LineMovementChart({ movement }: LineMovementChartProps): React.ReactElement {
  const series = movement.implied_prob_series
  const sideTitle = `${movement.side}${movement.book ? ` · ${movement.book}` : ''}`

  if (series.length < 2) {
    return (
      <div className={styles.wrap}>
        <div className={styles.header}>
          <span className={styles.title}>{sideTitle}</span>
        </div>
        <p className={styles.empty}>Not enough quotes to chart movement.</p>
      </div>
    )
  }

  const plotW = VB_W - PAD * 2
  const plotH = VB_H - PAD * 2
  const n = series.length
  const min = Math.min(...series)
  const max = Math.max(...series)
  const span = Math.max(1e-6, max - min)

  const x = (i: number): number => PAD + (i / (n - 1)) * plotW
  const y = (p: number): number => PAD + (1 - (p - min) / span) * plotH

  const points = series.map((p, i) => `${x(i).toFixed(1)},${y(p).toFixed(1)}`).join(' ')
  const closeX = x(n - 1)
  const closeY = y(series[n - 1])

  // Steam direction arrow: "up"/"down"/"flat" toward the chosen side.
  const dir = movement.direction
  const dirSymbol = dir === 'up' ? '▲' : dir === 'down' ? '▼' : '■'

  const clvProb =
    movement.clv && typeof movement.clv['clv_prob'] === 'number'
      ? (movement.clv['clv_prob'] as number)
      : null

  return (
    <div className={styles.wrap}>
      <div className={styles.header}>
        <span className={styles.title}>{sideTitle}</span>
        <span className={styles.badges}>
          {movement.sharp_consensus && <Badge variant="info">sharp</Badge>}
          {movement.has_movement && (
            <Badge variant="default">
              steam {dirSymbol}
            </Badge>
          )}
          {movement.beat_close && <Badge variant="success">beat close</Badge>}
        </span>
      </div>

      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className={styles.chart} role="img"
        aria-label={`Implied probability from open to close for ${sideTitle}`}>
        {/* open/close axis ticks */}
        <text x={PAD} y={VB_H - 4} className={styles.axisLabel} textAnchor="start">open</text>
        <text x={VB_W - PAD} y={VB_H - 4} className={styles.axisLabel} textAnchor="end">close</text>

        {/* series line */}
        <polyline points={points} className={styles.line} fill="none" />

        {/* close marker */}
        <line x1={closeX} y1={PAD} x2={closeX} y2={PAD + plotH} className={styles.closeMarker} />
        <circle cx={closeX} cy={closeY} r={3.5} className={styles.closeDot} />
      </svg>

      <div className={styles.footer}>
        <span>open {(series[0] * 100).toFixed(1)}%</span>
        <span>close {(series[n - 1] * 100).toFixed(1)}%</span>
        {clvProb != null && (
          <span className={clvProb >= 0 ? styles.clvPos : styles.clvNeg}>
            CLV {clvProb >= 0 ? '+' : ''}{(clvProb * 100).toFixed(1)}%
          </span>
        )}
      </div>
    </div>
  )
}
