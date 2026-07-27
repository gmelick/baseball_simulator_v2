/**
 * StatTile.tsx — SIM-439
 * A label + big monospace number + optional context line; KpiRow lays them out
 * in a responsive strip. DataFreshnessBadge shows the ingest watermark.
 */
import React from 'react'

import { Badge } from '@/components/ui'

import styles from './StatTile.module.css'

export interface StatTileProps {
  label: string
  value: React.ReactNode
  context?: React.ReactNode
  title?: string
}

export function StatTile({ label, value, context, title }: StatTileProps): React.ReactElement {
  return (
    <div className={styles.tile} title={title}>
      <span className={styles.label}>{label}</span>
      <span className={styles.value}>{value}</span>
      {context && <span className={styles.context}>{context}</span>}
    </div>
  )
}

export function KpiRow({ children }: { children: React.ReactNode }): React.ReactElement {
  return <div className={styles.row}>{children}</div>
}

export function DataFreshnessBadge({ date }: { date: string | null }): React.ReactElement {
  return (
    <Badge variant="default" aria-label="Data freshness">
      data as of {date ?? '—'}
    </Badge>
  )
}
