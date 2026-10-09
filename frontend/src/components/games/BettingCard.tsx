/**
 * BettingCard.tsx — SIM-395
 *
 * The betting surface for a game: per-market edge reports with the favored side
 * highlighted, +EV signal badges (stake% + offered price), and a mock-vs-real
 * `odds_source` indicator per market.
 * SIM-555: a market priced from the stored lines (the closing line; before the game
 * starts, also the latest current line) names the book of the fair price and, per
 * side, the book with the best price at that line.
 * SIM-546: the segment and team markets. The card renders every market the response
 * carries. It orders the sections by the response's `markets` (the vocabulary order)
 * and titles them from `market_names`. It groups them under four sub-headings: Full
 * game, First five innings, First inning, Team totals.
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
  type RunLinePricing,
  type SignalsResponse,
} from '@/api/betting'
import { Badge } from '@/components/ui'

import styles from './BettingCard.module.css'

export interface BettingCardProps {
  gamePk: number
}

// The edge label of a market -> its key in odds_source and fair_book (the API keys
// the run line's source 'runline' while its edges carry the label 'run_line').
const SOURCE_KEY: Record<string, string> = { run_line: 'runline' }
// SIM-546: the reverse. A market type in `markets` -> the label its edges carry.
const LABEL_OF: Record<string, string> = { runline: 'run_line' }
// SIM-546: the section titles when the response carries no `market_names` (an
// older API that priced only the three full-game markets).
const FALLBACK_NAMES: Record<string, string> = {
  moneyline: 'Moneyline',
  total: 'Total',
  run_line: 'Run line',
}
// SIM-546: the four sub-headings, in the order the card shows them.
const GROUPS = ['Full game', 'First five innings', 'First inning', 'Team totals'] as const
type Group = (typeof GROUPS)[number]
// SIM-546: each label's sub-heading, read from the market's segment and side in the
// vocabulary the API serves (pipeline/odds_provider.py). A team total of any segment
// sits under Team totals. The grouping is presentation, so it lives in the card.
const SEGMENT_OF: Record<string, Group> = {
  moneyline: 'Full game',
  run_line: 'Full game',
  total: 'Full game',
  first_to_score: 'Full game',
  f5_moneyline: 'First five innings',
  f5_total: 'First five innings',
  f5_runline: 'First five innings',
  f1_moneyline: 'First inning',
  f1_total: 'First inning',
  f1_runline: 'First inning',
  first_inning_run: 'First inning',
  team_total_home: 'Team totals',
  team_total_away: 'Team totals',
  f5_team_total_home: 'Team totals',
  f5_team_total_away: 'Team totals',
}
// SIM-546: the yes / no market (a run in the first inning) stores its two sides as
// over / under at 0.5. The card names them Yes / No.
const YES_NO_LABELS = new Set(['first_inning_run'])

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

function sideLabel(label: string, side: string, line: number | null): string {
  // SIM-546: a three-way market's third outcome is the tie.
  if (side === 'draw') return 'Tie'
  if (YES_NO_LABELS.has(label)) {
    if (side === 'over') return 'Yes'
    if (side === 'under') return 'No'
  }
  const base = side.charAt(0).toUpperCase() + side.slice(1)
  if (line == null) return base
  // Show the line for total/run-line sides (e.g. "Over 8.5", "Home -1.5").
  return `${base} ${line > 0 && (side === 'home' || side === 'away') ? '+' : ''}${line}`
}

/** SIM-546: the two-bets note's words for the two-way market the margin came from. */
function referenceName(source: string, names: Record<string, string>): string {
  if (source === 'total' || source === 'moneyline') return `the game's ${source} margin`
  const label = LABEL_OF[source] ?? source
  const name = names[label] ?? FALLBACK_NAMES[label] ?? source.replace(/_/g, ' ')
  return `the ${name.toLowerCase()} margin`
}

export function BettingCard({ gamePk }: BettingCardProps): React.ReactElement {
  const [edges, setEdges] = useState<EdgesResponse | null>(null)
  const [signals, setSignals] = useState<SignalsResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = (): void => {
    setLoading(true)
    setError(null)
    // In sequence, not in parallel: each request runs the same 200-game batch,
    // and the runner caches it (60 s). Fired together, both missed the cache and
    // ran two batches on one worker pool (45 s alone became ~100 s each), past
    // nginx's 120 s limit: a 504. Fired in turn, /signals reads /edges' batch.
    fetchEdges(gamePk, 200)
      .then(async (e) => [e, await fetchSignals(gamePk, 200)] as const)
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

  // SIM-546: the response's market order (the vocabulary order), then any label
  // the response did not list, in the order its edges arrived.
  const ordered = (edges.markets ?? []).map((m) => LABEL_OF[m] ?? m)
  const markets = [
    ...ordered.filter((m) => byMarket.has(m)),
    ...Array.from(byMarket.keys()).filter((m) => !ordered.includes(m)),
  ]
  const names = edges.market_names ?? {}
  // SIM-546: each sub-heading's markets, in market order. A label the table does
  // not know goes in a last group with no sub-heading.
  const grouped: Array<{ title: Group | null; markets: string[] }> = [
    ...GROUPS.map((g) => ({ title: g, markets: markets.filter((m) => SEGMENT_OF[m] === g) })),
    { title: null, markets: markets.filter((m) => SEGMENT_OF[m] === undefined) },
  ].filter((g) => g.markets.length > 0)

  // SIM-546: how a run line was priced, by label. An older API carries only the
  // full-game run line's pricing.
  const runLinePricing = (market: string): RunLinePricing | null | undefined =>
    edges.run_line_pricing_by_label?.[market] ??
    (market === 'run_line' ? edges.run_line_pricing : undefined)

  const renderMarket = (market: string): React.ReactElement => {
    const sides = byMarket.get(market) ?? []
    const source = edges.odds_source[SOURCE_KEY[market] ?? market]
    // SIM-555: a market priced from the stored lines names the one book whose
    // row gave the fair price.
    const fairBook = edges.fair_book_name?.[SOURCE_KEY[market] ?? market]
    const pricing = runLinePricing(market)
    const title = names[market] ?? FALLBACK_NAMES[market] ?? market
    const threeWay = sides.some((e) => e.side === 'draw')
    return (
      <section key={market} className={styles.market} aria-label={title}>
        <div className={styles.marketHeader}>
          <h5 className={styles.marketTitle}>{title}</h5>
          {source && (
            <Badge variant={source === 'mock' ? 'warning' : 'info'}>
              {source === 'mock' ? 'mock odds' : source === 'stored' ? 'stored odds' : 'live odds'}
            </Badge>
          )}
          {fairBook && <span className={styles.stake}>fair price from {fairBook}</span>}
        </div>

        {pricing?.shape === 'two_bets' && (
          <p className={styles.note}>
            Listed as two separate bets, not the two sides of one: each price is read
            over{' '}
            {pricing.reference_source === 'flat' || pricing.reference_source == null
              ? 'a flat 1.05 margin (no two-way market for this game)'
              : referenceName(pricing.reference_source, names)}
            .
          </p>
        )}

        {threeWay && (
          <p className={styles.note}>
            A tie is a priced outcome; the three fair prices add to one.
          </p>
        )}

        <div className={styles.sides}>
          {sides.map((e) => {
            const signal = signalByKey.get(`${e.label}:${e.side}`)
            return (
              <div
                key={e.side}
                className={`${styles.side} ${e.positive_edge ? styles.favored : ''}`}
              >
                <div className={styles.sideTop}>
                  <span className={styles.sideName}>{sideLabel(e.label, e.side, e.line)}</span>
                  <span className={styles.price}>{fmtAmerican(e.offered_american)}</span>
                </div>
                <div className={styles.metrics}>
                  <span>sim {fmtPct(e.sim_prob)}</span>
                  <span>fair {fmtPct(e.market_fair_prob)}</span>
                  <span className={e.edge > 0 ? styles.edgePos : styles.edgeNeg}>
                    edge {fmtSignedPct(e.edge)}
                  </span>
                  {/* SIM-555: the book with the best price at this line. */}
                  {e.price_book_name && <span>best at {e.price_book_name}</span>}
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
  }

  return (
    <div className={styles.card}>
      {grouped.map((g) => (
        <React.Fragment key={g.title ?? 'other'}>
          {g.title && <h4 className={styles.groupTitle}>{g.title}</h4>}
          {g.markets.map(renderMarket)}
        </React.Fragment>
      ))}
    </div>
  )
}
