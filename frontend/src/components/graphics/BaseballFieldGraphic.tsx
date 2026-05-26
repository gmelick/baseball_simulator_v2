/**
 * BaseballFieldGraphic.tsx — SIM-392
 *
 * A compact SVG of the field: the diamond, the 9 standard fielding positions,
 * the batter at the plate, and runner markers on occupied bases. Presentational
 * only — the Game page (SIM-393) feeds it the live baserunner state (SIM-386
 * on_1b/on_2b/on_3b) and, when known, player names.
 *
 * Label-collision rules:
 *   - Outfield position labels sit ABOVE their dot; infield labels BELOW — so
 *     a fielder label never overlaps the base/dot it annotates.
 *   - Runner name labels are anchored OUTSIDE the diamond at each base (right
 *     of 1B, above 2B, left of 3B) so they never collide with the base marker
 *     or the infield position labels.
 *   - The batter label sits below home plate, clear of the catcher dot.
 */
import React from 'react'

import styles from './BaseballFieldGraphic.module.css'

export type FieldPosition = 'P' | 'C' | '1B' | '2B' | 'SS' | '3B' | 'LF' | 'CF' | 'RF'

export interface BaseballFieldGraphicProps {
  /** Runner on first — a name (rendered as a label) or any truthy = occupied. */
  onFirst?: string | null
  onSecond?: string | null
  onThird?: string | null
  /** Current batter name (label under home plate). */
  batter?: string | null
  /** Optional fielder names keyed by position. */
  fielders?: Partial<Record<FieldPosition, string>>
  /** Show the P/C/1B… position labels (default true). */
  showPositionLabels?: boolean
  /** Accessible description; defaults to a generated base-state summary. */
  ariaLabel?: string
}

// viewBox 0 0 300 300. Home at the bottom, second at the top.
const BASES = {
  home: { x: 150, y: 248 },
  first: { x: 228, y: 170 },
  second: { x: 150, y: 92 },
  third: { x: 72, y: 170 },
} as const

const FIELDERS: Record<FieldPosition, { x: number; y: number; outfield: boolean }> = {
  P: { x: 150, y: 178, outfield: false },
  C: { x: 150, y: 270, outfield: false },
  '1B': { x: 205, y: 150, outfield: false },
  '2B': { x: 182, y: 118, outfield: false },
  SS: { x: 118, y: 118, outfield: false },
  '3B': { x: 95, y: 150, outfield: false },
  LF: { x: 70, y: 52, outfield: true },
  CF: { x: 150, y: 30, outfield: true },
  RF: { x: 230, y: 52, outfield: true },
}

function Base({
  x,
  y,
  occupied,
}: {
  x: number
  y: number
  occupied: boolean
}): React.ReactElement {
  // A 16px square rotated 45° (a diamond) centered on (x, y).
  const s = 9
  return (
    <rect
      x={x - s}
      y={y - s}
      width={s * 2}
      height={s * 2}
      transform={`rotate(45 ${x} ${y})`}
      className={occupied ? styles.baseOccupied : styles.baseEmpty}
    />
  )
}

export function BaseballFieldGraphic({
  onFirst,
  onSecond,
  onThird,
  batter,
  fielders,
  showPositionLabels = true,
  ariaLabel,
}: BaseballFieldGraphicProps): React.ReactElement {
  const first = onFirst != null && onFirst !== ''
  const second = onSecond != null && onSecond !== ''
  const third = onThird != null && onThird !== ''

  const baseSummary =
    [first && '1st', second && '2nd', third && '3rd'].filter(Boolean).join(', ') ||
    'bases empty'
  const label = ariaLabel ?? `Field: runners on ${baseSummary}`

  // Runner labels are only drawn when a name string (not just `true`) is given.
  const runnerName = (v: string | null | undefined): string | null =>
    typeof v === 'string' && v !== '' ? v : null

  return (
    <svg
      viewBox="0 0 300 300"
      className={styles.field}
      role="img"
      aria-label={label}
    >
      {/* Outfield arc + infield grass */}
      <path
        d="M 30 200 A 170 170 0 0 1 270 200 L 150 248 Z"
        className={styles.outfield}
      />
      {/* Infield diamond (base paths) */}
      <polygon
        points={`${BASES.home.x},${BASES.home.y} ${BASES.first.x},${BASES.first.y} ${BASES.second.x},${BASES.second.y} ${BASES.third.x},${BASES.third.y}`}
        className={styles.infield}
      />

      {/* Bases */}
      <Base x={BASES.first.x} y={BASES.first.y} occupied={first} />
      <Base x={BASES.second.x} y={BASES.second.y} occupied={second} />
      <Base x={BASES.third.x} y={BASES.third.y} occupied={third} />
      {/* Home plate (pentagon-ish: just a small marker) */}
      <circle cx={BASES.home.x} cy={BASES.home.y} r={5} className={styles.homePlate} />

      {/* Fielders */}
      {(Object.keys(FIELDERS) as FieldPosition[]).map((pos) => {
        const f = FIELDERS[pos]
        const name = fielders?.[pos]
        const labelDy = f.outfield ? -12 : 16 // above for OF, below for IF
        return (
          <g key={pos}>
            <circle cx={f.x} cy={f.y} r={5} className={styles.fielder} />
            {showPositionLabels && (
              <text x={f.x} y={f.y + labelDy} className={styles.posLabel}>
                {pos}
              </text>
            )}
            {name != null && name !== '' && (
              <text x={f.x} y={f.y + labelDy + (f.outfield ? -10 : 12)} className={styles.nameLabel}>
                {name}
              </text>
            )}
          </g>
        )
      })}

      {/* Runner name labels — anchored clear of the diamond. */}
      {runnerName(onFirst) != null && (
        <text x={BASES.first.x + 14} y={BASES.first.y} textAnchor="start" className={styles.runnerLabel}>
          {runnerName(onFirst)}
        </text>
      )}
      {runnerName(onSecond) != null && (
        <text x={BASES.second.x} y={BASES.second.y - 16} textAnchor="middle" className={styles.runnerLabel}>
          {runnerName(onSecond)}
        </text>
      )}
      {runnerName(onThird) != null && (
        <text x={BASES.third.x - 14} y={BASES.third.y} textAnchor="end" className={styles.runnerLabel}>
          {runnerName(onThird)}
        </text>
      )}

      {/* Batter */}
      {batter != null && batter !== '' && (
        <text x={BASES.home.x} y={BASES.home.y + 22} textAnchor="middle" className={styles.batterLabel}>
          {batter}
        </text>
      )}
    </svg>
  )
}
