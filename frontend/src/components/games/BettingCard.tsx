/**
 * BettingCard.tsx — SIM-395
 *
 * The betting surface for a game: per-market (moneyline / total / run-line)
 * edge reports with the favored side highlighted, +EV signal badges (stake% +
 * offered price), and a mock-vs-real `odds_source` indicator per market.
 *
 * Gated behind a "Load betting" button (the /edges + /signals endpoints run a
 * sim). Edges + signals are fetched together; signals are matched onto their
 * source edge by label+side.
 */
import React, { useState } from 'react'

import {
  fetchEdges,
  fetchSignals,
  type BetSignal,
  type EdgeReport,
  type EdgesResponse,
  type SignalsResponse,
} from '@/api/betting'
import { Badge } from '@/components/ui'

import styles from './BettingCard.module.css'

export interface BettingCardProps {
  gamePk: number
}

const MARKET_ORDER = ['moneyline', 'total', 'run_line']
const MARKET_LABELS: Record<string, string> = {
  moneyline: 'Moneyline',
  total: 'Total',
  run_line: 'Run line',
}

function fmtAmerican(a: number | null | undefined): string {
  if (a == null) return '—'
  const r = Math.round(a)
  return r > 0 ? `+${r}` : String(r)
}

function fmtPct(p: number | null | undefined): string {
  return p == null ? '—' : `${(p * 100).toFixed(1)}%`
}

function fmtSignedPct(p: number): string {
  const v = (p * 100).toFixed(1)
  return p > 0 ? `+${v}%` : `${v}%`
}

function sideLabel(side: string, line: number | null): string {
  const base = side.charAt(0).toUpperCase() + side.slice(1)
  if (line == null) return base
  // Show the line for total/run-line sides (e.g. "Over 8.5", "Home -1.5").
  return `${base} ${line > 0 && (side === 'home' || side === 'away') ? '+' : ''}${line}`
}

export function BettingCard({ gamePk }: BettingCardProps): React.ReactElement {
  const [edges, setEdges] = useState<EdgesResponse | null>(null)
  const [signals, setSignals] = useState<SignalsResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = (): void => {
    setLoading(true)
    setError(null)
    Promise.all([fetchEdges(gamePk, 200), fetchSignals(gamePk, 200)])
      .then(([e, s]) => {
        setEdges(e)
        setSignals(s)
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : 'Failed to load betting data.'),
      )
      .finally(() => setLoading(false))
  }

  if (!edges) {
    return (
      <div className={styles.gate}>
        <button type="button" className={styles.loadButton} onClick={load} disabled={loading}>
          {loading ? 'Running sim + pricing markets…' : 'Load betting'}
        </button>
        {error && <p className={styles.error} role="alert">{error}</p>}
      </div>
    )
  }

  // Group edges by market label; index signals by label:side.
  const byMarket = new Map<string, EdgeReport[]>()
  for (const e of edges.edges) {
    const arr = byMarket.get(e.label)
    if (arr) arr.push(e)
    else byMarket.set(e.label, [e])
  }
  const signalByKey = new Map<string, BetSignal>()
  for (const s of signals?.signals ?? []) {
    signalByKey.set(`${s.label}:${s.side}`, s)
  }

  const markets = [
    ...MARKET_ORDER.filter((m) => byMarket.has(m)),
    ...Array.from(byMarket.keys()).filter((m) => !MARKET_ORDER.includes(m)),
  ]

  return (
    <div className={styles.card}>
      {markets.map((market) => {
        const sides = byMarket.get(market) ?? []
        const source = edges.odds_source[market]
        return (
          <section key={market} className={styles.market}>
            <div className={styles.marketHeader}>
              <h4 className={styles.marketTitle}>{MARKET_LABELS[market] ?? market}</h4>
              {source && (
                <Badge variant={source === 'mock' ? 'warning' : 'info'}>
                  {source === 'mock' ? 'mock odds' : 'live odds'}
                </Badge>
              )}
            </div>

            <div className={styles.sides}>
              {sides.map((e) => {
                const signal = signalByKey.get(`${e.label}:${e.side}`)
                return (
                  <div
                    key={e.side}
                    className={`${styles.side} ${e.positive_edge ? styles.favored : ''}`}
                  >
                    <div className={styles.sideTop}>
                      <span className={styles.sideName}>{sideLabel(e.side, e.line)}</span>
                      <span className={styles.price}>{fmtAmerican(e.offered_american)}</span>
                    </div>
                    <div className={styles.metrics}>
                      <span>sim {fmtPct(e.sim_prob)}</span>
                      <span>fair {fmtPct(e.market_fair_prob)}</span>
                      <span className={e.edge > 0 ? styles.edgePos : styles.edgeNeg}>
                        edge {fmtSignedPct(e.edge)}
                      </span>
                    </div>
                    {signal && (
                      <div className={styles.signal}>
                        <Badge variant="success">+EV</Badge>
                        <span className={styles.stake}>
                          stake {(signal.stake_fraction * 100).toFixed(1)}%
                        </span>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </section>
        )
      })}
    </div>
  )
}
