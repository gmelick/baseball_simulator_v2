/**
 * CompTable.tsx — SIM-439
 * The ranked comps grid: rank, player (→ /player/:id), season, composite bar,
 * per-sub-score mini-bars, and the sample size that drives the confidence
 * discount. A row click opens the pair drill.
 */
import React from 'react'
import { Link } from 'react-router-dom'

import type { SimMember, SubScoreMeta } from '@/api/similarity'
import { ScoreBar } from '@/components/charts'

import styles from './CompTable.module.css'

export interface CompTableProps {
  members: SimMember[]
  meta: SubScoreMeta[]
  sampleField: string
  minSample: number
  onRowClick?: (m: SimMember) => void
}

export function CompTable({ members, meta, sampleField, minSample, onRowClick }: CompTableProps): React.ReactElement {
  if (members.length === 0) {
    return <p className={styles.empty}>No comparable seasons.</p>
  }
  return (
    <div className={styles.scroller}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th className={styles.thRank}>#</th>
            <th className={styles.th}>Player</th>
            <th className={styles.thNum}>Season</th>
            <th className={styles.thScore}>Composite</th>
            {meta.map((m) => (
              <th key={m.field} className={styles.thSub} title={`${m.label}${m.weight != null ? ` · ${Math.round(m.weight * 100)}%` : ''}`}>
                {m.label}
              </th>
            ))}
            <th className={styles.thNum} title={sampleField}>
              Sample
            </th>
          </tr>
        </thead>
        <tbody>
          {members.map((m, i) => {
            const thin = m.sample < minSample
            return (
              <tr key={`${m.entity_id}-${m.season}`} className={styles.tr} onClick={() => onRowClick?.(m)} tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') onRowClick?.(m)
                }}
              >
                <td className={styles.rank}>{i + 1}</td>
                <td className={styles.name}>
                  <Link className={styles.link} to={`/player/${m.entity_id}`} onClick={(e) => e.stopPropagation()}>
                    {m.name}
                  </Link>
                </td>
                <td className={styles.num}>{m.season}</td>
                <td className={styles.scoreCell}>
                  <ScoreBar value={m.score} />
                </td>
                {meta.map((sm) => (
                  <td key={sm.field} className={styles.subCell}>
                    <ScoreBar value={m.sub_scores[sm.field] ?? 0} showValue={false} tone="muted" />
                    <span className={styles.subVal}>{(m.sub_scores[sm.field] ?? 0).toFixed(2)}</span>
                  </td>
                ))}
                <td className={`${styles.num} ${thin ? styles.thin : ''}`} title={thin ? 'below the engine minimum' : undefined}>
                  {m.sample.toLocaleString()}
                  {thin && ' ⚠'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
