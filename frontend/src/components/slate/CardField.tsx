/**
 * CardField.tsx — the design's "On the field" diagram (SIM-519 Part I).
 *
 * The SVG is the design's field (viewBox 0 -10 400 310). Each fielder's
 * label sits at the design's point for his position; an occupied base turns
 * red and its runner's label sits beside it; the batter's label sits at the
 * plate. Positions are percentages of the box, so the diagram scales.
 */
import React from 'react'

import type { FeedLive } from '@/api/games'

import styles from './CardField.module.css'
import { lastName } from './format'

/** The design's label points, in SVG units. */
const FIELD_POINTS: Record<string, [number, number]> = {
  P: [200, 175],
  C: [200, 292],
  '1B': [304, 150],
  '2B': [262, 112],
  SS: [138, 112],
  '3B': [96, 150],
  LF: [78, 58],
  CF: [200, 26],
  RF: [322, 58],
}
const RUNNER_POINTS: Record<'first' | 'second' | 'third', [number, number]> = {
  first: [318, 196],
  second: [200, 80],
  third: [82, 196],
}
const RUNNER_TAG = { first: '1B', second: '2B', third: '3B' } as const

const pct = ([x, y]: [number, number]): React.CSSProperties => ({ left: `${x / 4}%`, top: `${(y + 10) / 3.1}%` })

export function CardField({ live }: { live: FeedLive }): React.ReactElement {
  const r = live.runners
  return (
    <div className={styles.field} role="img" aria-label={fieldDescription(live)}>
      <svg viewBox="0 -10 400 310" preserveAspectRatio="none" className={styles.svg} aria-hidden="true">
        <path d="M200 250 L17.6 67.6 A258 258 0 0 1 382.4 67.6 Z" fill="var(--dd-grass)" />
        <path d="M17.6 67.6 A258 258 0 0 1 382.4 67.6" fill="none" stroke="var(--dd-track)" strokeWidth="9" />
        <path d="M200 250 L306.3 143.7 A112 112 0 0 0 93.7 143.7 Z" fill="var(--dd-dirt)" />
        <path d="M200 238 L264 175 L200 112 L136 175 Z" fill="var(--dd-grass-light)" />
        <circle cx="200" cy="179" r="11" fill="var(--dd-dirt)" />
        <circle cx="200" cy="250" r="17" fill="var(--dd-dirt)" />
        <line x1="200" y1="250" x2="17.6" y2="67.6" stroke="var(--dd-line)" strokeWidth="2" />
        <line x1="200" y1="250" x2="382.4" y2="67.6" stroke="var(--dd-line)" strokeWidth="2" />
        <rect x="197" y="176" width="6" height="2" fill="var(--dd-line)" />
      </svg>
      <div className={`${styles.base} ${styles.first} ${r.first ? styles.occupied : ''}`} />
      <div className={`${styles.base} ${styles.second} ${r.second ? styles.occupied : ''}`} />
      <div className={`${styles.base} ${styles.third} ${r.third ? styles.occupied : ''}`} />
      <div className={styles.plate} />

      {Object.entries(FIELD_POINTS).map(([pos, xy]) => {
        const name = live.fielders[pos]
        if (!name) return null
        return (
          <div key={pos} className={styles.label} style={pct(xy)}>
            <span className={styles.labelTag}>{pos}</span>
            {lastName(name)}
          </div>
        )
      })}
      {(Object.keys(RUNNER_POINTS) as Array<keyof typeof RUNNER_POINTS>).map((base) => {
        const runner = r[base]
        if (!runner) return null
        return (
          <div key={base} className={`${styles.label} ${styles.runner}`} style={pct(RUNNER_POINTS[base])}>
            <span className={styles.labelTag}>{RUNNER_TAG[base]}</span>
            {lastName(runner.name)}
          </div>
        )
      })}
      {live.batter && (
        <div className={`${styles.label} ${styles.batter}`} style={pct([200, 260])}>
          <span className={styles.labelTag}>AB</span>
          {lastName(live.batter.name)}
        </div>
      )}
    </div>
  )
}

function fieldDescription(live: FeedLive): string {
  const on = (['first', 'second', 'third'] as const).filter((b) => live.runners[b])
  const bases = on.length === 0 ? 'Bases empty' : `Runners on ${on.join(' and ')}`
  return `${bases}. ${live.batter ? `${live.batter.name} at bat` : ''}`.trim()
}
