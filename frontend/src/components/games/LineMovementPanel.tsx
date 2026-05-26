/**
 * LineMovementPanel.tsx — SIM-396
 *
 * Fetches the stored opening→closing line-movement history for a market and
 * renders one LineMovementChart per (side, book) series. A market toggle
 * switches between moneyline / total / run-line. The /line-movement endpoint is
 * a cheap DB read (no sim), so this fetches on mount; an empty history (no
 * stored odds) renders a friendly message.
 */
import React, { useEffect, useState } from 'react'

import { fetchLineMovement, type LineMovementResponse } from '@/api/betting'

import { LineMovementChart } from './LineMovementChart'
import styles from './LineMovementPanel.module.css'

export interface LineMovementPanelProps {
  gamePk: number
}

const MARKETS: Array<{ key: string; label: string }> = [
  { key: 'moneyline', label: 'Moneyline' },
  { key: 'total', label: 'Total' },
  { key: 'run_line', label: 'Run line' },
]

export function LineMovementPanel({ gamePk }: LineMovementPanelProps): React.ReactElement {
  const [market, setMarket] = useState('moneyline')
  const [data, setData] = useState<LineMovementResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchLineMovement(gamePk, market)
      .then((d) => !cancelled && setData(d))
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Failed to load line movement.')
      })
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [gamePk, market])

  return (
    <div className={styles.panel}>
      <div className={styles.tabs} role="tablist" aria-label="Market">
        {MARKETS.map((m) => (
          <button
            key={m.key}
            type="button"
            role="tab"
            aria-selected={market === m.key}
            className={`${styles.tab} ${market === m.key ? styles.tabActive : ''}`}
            onClick={() => setMarket(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>

      {loading && <p className={styles.muted}>Loading line movement…</p>}
      {error && <p className={styles.error} role="alert">{error}</p>}

      {!loading && !error && (data?.series.length ?? 0) === 0 && (
        <p className={styles.muted}>No stored line-movement history for this market.</p>
      )}

      {!loading && !error && data && data.series.length > 0 && (
        <div className={styles.charts}>
          {data.series.map((m, i) => (
            <LineMovementChart key={`${m.side}-${m.book ?? 'na'}-${i}`} movement={m} />
          ))}
        </div>
      )}
    </div>
  )
}
