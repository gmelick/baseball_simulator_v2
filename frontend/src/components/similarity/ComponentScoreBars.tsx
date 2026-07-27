/**
 * ComponentScoreBars.tsx — SIM-439
 * The core breakdown: one weighted bar per component sub-score (fill = the raw
 * 0..1 value, a faint tick at the sub-score's weight), a composite header, and
 * the honest footnote that the composite is discounted by sample confidence —
 * NOT Σ(weight·sub-score). Handles 1/2/3/4-bar engines.
 */
import React from 'react'

import type { SubScoreMeta } from '@/api/similarity'
import { ScoreBar } from '@/components/charts'
import { Badge } from '@/components/ui'

import styles from './ComponentScoreBars.module.css'

export interface ComponentScoreBarsProps {
  meta: SubScoreMeta[]
  subScores: Record<string, number>
  composite?: number
  sample?: number
  sampleField?: string
  minSample?: number
  note?: string
  compact?: boolean
}

export function ComponentScoreBars({
  meta,
  subScores,
  composite,
  sample,
  sampleField,
  minSample,
  note,
  compact = false,
}: ComponentScoreBarsProps): React.ReactElement {
  const thin = minSample != null && sample != null && sample < minSample
  return (
    <div className={styles.wrap}>
      {composite != null && (
        <div className={styles.compositeRow}>
          <span className={styles.compositeLabel}>Composite similarity</span>
          <span className={styles.compositeVal}>{composite.toFixed(3)}</span>
          {thin && (
            <Badge variant="warning" aria-label="Below minimum sample">
              thin sample
            </Badge>
          )}
        </div>
      )}
      <div className={styles.bars}>
        {meta.map((m) => (
          <div key={m.field} className={styles.barRow}>
            <span className={styles.barLabel}>
              {m.label}
              {m.weight != null && <span className={styles.weight}>{Math.round(m.weight * 100)}%</span>}
            </span>
            <ScoreBar value={subScores[m.field] ?? 0} weight={m.weight ?? undefined} />
          </div>
        ))}
      </div>
      {!compact && (
        <p className={styles.footnote}>
          {sample != null && sampleField && (
            <>
              Sample: <span className={styles.mono}>{sample.toLocaleString()}</span> {sampleField.replace(/_/g, ' ')}
              {minSample != null && ` (engine min ${minSample.toLocaleString()})`}.{' '}
            </>
          )}
          The composite is discounted by sample confidence (√ of the smaller Empirical-Bayes weight) — it is{' '}
          <em>not</em> the weighted sum of these bars.
          {note && <span className={styles.note}> {note}</span>}
        </p>
      )}
    </div>
  )
}
