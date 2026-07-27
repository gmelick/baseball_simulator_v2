/**
 * charts/index.tsx — SIM-439
 *
 * One hand-rolled chart system for the Data Lab + Similarity Explorer. No chart
 * library — pure SVG (fixed viewBox, tokenized fills) + a little CSS, matching
 * the PropDistributionChart house style. Every chart is theme-safe (uses only
 * --sim-* semantic/primary tokens), labels values in-mark (never hue-only), and
 * carries role="img" + an aria-label.
 *
 * Exports: ScoreBar, BarList, ColumnChart, HeatGrid, RadarChart.
 */
import React from 'react'

import styles from './charts.module.css'

const clamp01 = (v: number): number => Math.max(0, Math.min(1, v))

// ---------------------------------------------------------------------------
// ScoreBar — inline 0..1 bar cell (similarity scores, sub-scores)
// ---------------------------------------------------------------------------

export interface ScoreBarProps {
  value: number
  /** Show the numeric value at the end of the track. */
  showValue?: boolean
  /** 0..1 weight track drawn behind the fill (component-score weight). */
  weight?: number
  tone?: 'primary' | 'muted' | 'success'
  ariaLabel?: string
}

export function ScoreBar({ value, showValue = true, weight, tone = 'primary', ariaLabel }: ScoreBarProps): React.ReactElement {
  const pct = clamp01(value) * 100
  return (
    <span className={styles.scoreBar} role="img" aria-label={ariaLabel ?? `score ${value.toFixed(3)}`}>
      <span className={styles.scoreTrack}>
        {weight != null && <span className={styles.weightTick} style={{ left: `${clamp01(weight) * 100}%` }} />}
        <span className={`${styles.scoreFill} ${styles[`tone-${tone}`]}`} style={{ width: `${pct}%` }} />
      </span>
      {showValue && <span className={styles.scoreVal}>{value.toFixed(3)}</span>}
    </span>
  )
}

// ---------------------------------------------------------------------------
// BarList — horizontal labeled bars (pitch mix, PA outcomes, by-season, boards)
// ---------------------------------------------------------------------------

export interface BarListItem {
  label: string
  value: number
  sub?: string
  highlight?: boolean
}

export interface BarListProps {
  items: BarListItem[]
  formatValue?: (v: number) => string
  ariaLabel?: string
  maxItems?: number
}

export function BarList({ items, formatValue, ariaLabel, maxItems }: BarListProps): React.ReactElement {
  const shown = maxItems != null ? items.slice(0, maxItems) : items
  const max = Math.max(1e-9, ...shown.map((i) => i.value))
  const fmt = formatValue ?? ((v: number) => String(v))
  if (shown.length === 0) return <p className={styles.empty}>No data.</p>
  return (
    <div className={styles.barList} role="img" aria-label={ariaLabel ?? 'bar chart'}>
      {shown.map((it, i) => (
        <div key={`${it.label}-${i}`} className={styles.barRow}>
          <span className={styles.barLabel} title={it.label}>
            {it.label}
            {it.sub && <span className={styles.barSub}>{it.sub}</span>}
          </span>
          <span className={styles.barTrack}>
            <span
              className={`${styles.barFill} ${it.highlight ? styles.barFillHi : ''}`}
              style={{ width: `${(it.value / max) * 100}%` }}
            />
          </span>
          <span className={styles.barValue}>{fmt(it.value)}</span>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ColumnChart — vertical SVG bars over a numeric x (velo histogram, score pop.)
// ---------------------------------------------------------------------------

export interface ColumnPoint {
  x: number
  value: number
  highlight?: boolean
}

export interface ColumnMarker {
  x: number
  label: string
}

export interface ColumnChartProps {
  data: ColumnPoint[]
  xDomain?: [number, number]
  xLabel?: string
  yLabel?: string
  markers?: ColumnMarker[]
  ariaLabel?: string
  height?: number
}

const VB_W = 360
const PAD_L = 34
const PAD_R = 10
const PAD_T = 10
const PAD_B = 26

export function ColumnChart({
  data,
  xDomain,
  xLabel,
  markers = [],
  ariaLabel,
  height = 170,
}: ColumnChartProps): React.ReactElement {
  if (data.length === 0) return <p className={styles.empty}>No data.</p>
  const VB_H = height
  const plotW = VB_W - PAD_L - PAD_R
  const plotH = VB_H - PAD_T - PAD_B

  const xs = data.map((d) => d.x)
  const minX = xDomain ? xDomain[0] : Math.min(...xs)
  const maxX = xDomain ? xDomain[1] : Math.max(...xs)
  const spanX = Math.max(1e-9, maxX - minX)
  const maxV = Math.max(1e-9, ...data.map((d) => d.value))
  const slot = plotW / Math.max(1, data.length)
  const barW = Math.max(1.5, slot * 0.82)

  const xToPx = (x: number): number => PAD_L + ((x - minX) / spanX) * plotW

  return (
    <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className={styles.svg} role="img" aria-label={ariaLabel ?? 'histogram'}>
      <line x1={PAD_L} y1={PAD_T + plotH} x2={VB_W - PAD_R} y2={PAD_T + plotH} className={styles.axis} />
      {data.map((d, i) => {
        const h = (d.value / maxV) * plotH
        const cx = PAD_L + (i + 0.5) * slot
        return (
          <rect
            key={i}
            x={cx - barW / 2}
            y={PAD_T + plotH - h}
            width={barW}
            height={h}
            className={d.highlight ? styles.colBarHi : styles.colBar}
          >
            <title>{`${d.x}: ${d.value}`}</title>
          </rect>
        )
      })}
      {markers.map((m, i) => {
        const mx = xToPx(m.x)
        return (
          <g key={`m-${i}`}>
            <line x1={mx} y1={PAD_T} x2={mx} y2={PAD_T + plotH} className={styles.marker} />
            <text x={mx} y={PAD_T + 8} className={styles.markerLabel} textAnchor="middle">
              {m.label}
            </text>
          </g>
        )
      })}
      {/* x-axis min/mid/max ticks */}
      {[minX, (minX + maxX) / 2, maxX].map((tx, i) => (
        <text key={`t-${i}`} x={xToPx(tx)} y={VB_H - 8} className={styles.tick} textAnchor="middle">
          {Number.isInteger(tx) ? tx : tx.toFixed(2)}
        </text>
      ))}
      {xLabel && (
        <text x={PAD_L + plotW / 2} y={VB_H - 0.5} className={styles.axisLabel} textAnchor="middle">
          {xLabel}
        </text>
      )}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// HeatGrid — CSS grid of cells with intensity (count-state, zone)
// ---------------------------------------------------------------------------

export interface HeatCell {
  value: number | null
  label: string
  title?: string
}

export interface HeatGridProps {
  rows: number
  cols: number
  cell: (r: number, c: number) => HeatCell | null
  rowLabels?: string[]
  colLabels?: string[]
  ariaLabel?: string
}

export function HeatGrid({ rows, cols, cell, rowLabels, colLabels, ariaLabel }: HeatGridProps): React.ReactElement {
  // Normalize intensities over the present values.
  const vals: number[] = []
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const cc = cell(r, c)
      if (cc && cc.value != null) vals.push(cc.value)
    }
  }
  const min = vals.length ? Math.min(...vals) : 0
  const max = vals.length ? Math.max(...vals) : 1
  const span = Math.max(1e-9, max - min)

  return (
    <div
      className={styles.heatWrap}
      role="img"
      aria-label={ariaLabel ?? 'heat grid'}
      style={{ gridTemplateColumns: `${rowLabels ? 'auto ' : ''}repeat(${cols}, 1fr)` }}
    >
      {colLabels && rowLabels && <span className={styles.heatCorner} />}
      {colLabels &&
        colLabels.map((cl, c) => (
          <span key={`c-${c}`} className={styles.heatColLabel}>
            {cl}
          </span>
        ))}
      {Array.from({ length: rows }).map((_, r) => (
        <React.Fragment key={`r-${r}`}>
          {rowLabels && <span className={styles.heatRowLabel}>{rowLabels[r]}</span>}
          {Array.from({ length: cols }).map((__, c) => {
            const cc = cell(r, c)
            const t = cc && cc.value != null ? 0.12 + 0.88 * ((cc.value - min) / span) : 0
            return (
              <span
                key={`cell-${r}-${c}`}
                className={styles.heatCell}
                title={cc?.title}
                style={{ ['--heat' as string]: String(t) }}
              >
                {cc ? cc.label : ''}
              </span>
            )
          })}
        </React.Fragment>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// RadarChart — SVG polygon over N axes (component sub-scores; pair overlay)
// ---------------------------------------------------------------------------

export interface RadarAxis {
  label: string
  value: number // 0..1
  compare?: number // 0..1 optional overlay
}

export interface RadarChartProps {
  axes: RadarAxis[]
  ariaLabel?: string
  size?: number
}

export function RadarChart({ axes, ariaLabel, size = 220 }: RadarChartProps): React.ReactElement {
  const n = axes.length
  if (n < 3) return <p className={styles.empty}>Radar needs 3+ dimensions.</p>
  const cx = size / 2
  const cy = size / 2
  const R = size / 2 - 34

  const point = (i: number, v: number): [number, number] => {
    const ang = -Math.PI / 2 + (i * 2 * Math.PI) / n
    const r = clamp01(v) * R
    return [cx + r * Math.cos(ang), cy + r * Math.sin(ang)]
  }
  const poly = (key: 'value' | 'compare'): string =>
    axes
      .map((a, i) => {
        const v = key === 'value' ? a.value : (a.compare ?? 0)
        const [x, y] = point(i, v)
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' ')

  const hasCompare = axes.some((a) => a.compare != null)

  return (
    <svg viewBox={`0 0 ${size} ${size}`} className={styles.svg} role="img" aria-label={ariaLabel ?? 'radar chart'}>
      {[0.25, 0.5, 0.75, 1].map((ring) => (
        <polygon
          key={ring}
          className={styles.radarRing}
          points={axes.map((_, i) => point(i, ring).map((n2) => n2.toFixed(1)).join(',')).join(' ')}
        />
      ))}
      {axes.map((_, i) => {
        const [x, y] = point(i, 1)
        return <line key={`spoke-${i}`} x1={cx} y1={cy} x2={x} y2={y} className={styles.radarSpoke} />
      })}
      {hasCompare && <polygon className={styles.radarCompare} points={poly('compare')} />}
      <polygon className={styles.radarArea} points={poly('value')} />
      {axes.map((a, i) => {
        const [lx, ly] = point(i, 1.16)
        return (
          <text key={`lbl-${i}`} x={lx} y={ly} className={styles.radarLabel} textAnchor="middle">
            {a.label}
          </text>
        )
      })}
    </svg>
  )
}
