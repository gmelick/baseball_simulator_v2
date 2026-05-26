/**
 * PropDistributionChart.tsx — SIM-394
 *
 * A compact bar chart of a prop's PMF (one bar per integer outcome in the
 * compact support), with an optional dashed vertical prop-line marker and a
 * mean marker. Bars at/over the line are tinted so the over/under split reads
 * at a glance. Pure SVG, responsive via viewBox.
 *
 * Driven by a SIM-390 PropEdge payload's `support` + `probabilities` arrays.
 */
import React from 'react'

import styles from './PropDistributionChart.module.css'

export interface PropDistributionChartProps {
  /** Sorted integer outcomes (the compact PMF support). */
  support: number[]
  /** Aligned probabilities (sum to 1). */
  probabilities: number[]
  /** Optional betting line; draws a dashed marker and tints the over bars. */
  line?: number | null
  /** Optional mean marker. */
  mean?: number | null
  /** Axis label (e.g. "K", "H"). */
  propLabel?: string
}

const VB_W = 320
const VB_H = 160
const PAD_L = 8
const PAD_R = 8
const PAD_T = 10
const PAD_B = 22

export function PropDistributionChart({
  support,
  probabilities,
  line,
  mean,
  propLabel,
}: PropDistributionChartProps): React.ReactElement {
  if (support.length === 0) {
    return <p className={styles.empty}>No distribution data.</p>
  }

  const plotW = VB_W - PAD_L - PAD_R
  const plotH = VB_H - PAD_T - PAD_B
  const n = support.length
  const slotW = plotW / n
  const barW = Math.max(2, slotW * 0.7)
  const maxP = Math.max(...probabilities, 1e-9)

  // Map a support value (or fractional line) to an x pixel at the slot center.
  const minV = support[0]
  const maxV = support[n - 1]
  const span = Math.max(1, maxV - minV)
  const xForValue = (v: number): number =>
    PAD_L + ((v - minV) / span) * (plotW - slotW) + slotW / 2

  return (
    <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className={styles.chart} role="img"
      aria-label={`Distribution of ${propLabel ?? 'prop'}${line != null ? `, line ${line}` : ''}`}>
      {/* baseline */}
      <line x1={PAD_L} y1={PAD_T + plotH} x2={VB_W - PAD_R} y2={PAD_T + plotH} className={styles.axis} />

      {/* bars */}
      {support.map((v, i) => {
        const p = probabilities[i] ?? 0
        const h = (p / maxP) * plotH
        const cx = xForValue(v)
        const x = cx - barW / 2
        const y = PAD_T + plotH - h
        const over = line != null && v > line
        return (
          <g key={v}>
            <rect
              x={x}
              y={y}
              width={barW}
              height={h}
              className={over ? styles.barOver : styles.bar}
            />
            <text x={cx} y={VB_H - 8} className={styles.tick}>
              {v}
            </text>
          </g>
        )
      })}

      {/* mean marker */}
      {mean != null && (
        <line
          x1={xForValue(mean)}
          y1={PAD_T}
          x2={xForValue(mean)}
          y2={PAD_T + plotH}
          className={styles.meanMarker}
        />
      )}

      {/* prop-line marker (between integer bars for a half-integer line) */}
      {line != null && (
        <line
          x1={xForValue(line)}
          y1={PAD_T - 2}
          x2={xForValue(line)}
          y2={PAD_T + plotH}
          className={styles.lineMarker}
        />
      )}
    </svg>
  )
}
