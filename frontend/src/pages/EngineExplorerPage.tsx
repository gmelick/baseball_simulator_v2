/**
 * EngineExplorerPage.tsx — SIM-439
 * Search a subject, see the ranked comps + the score-population distribution,
 * and drill into any comp's component-score breakdown. Subject / position /
 * vs-hand / compare all live in the URL for shareable deep-links.
 */
import React from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import {
  fetchSimilarity,
  fetchSimilarityPair,
  type SimMember,
} from '@/api/similarity'
import { ColumnChart, RadarChart } from '@/components/charts'
import { EmptyState, EngineWarmingState, ErrorBlock, LoadingBlock } from '@/components/lab/states'
import { CompTable } from '@/components/similarity/CompTable'
import { ComponentScoreBars } from '@/components/similarity/ComponentScoreBars'
import { PlayerSearchInput } from '@/components/similarity/PlayerSearchInput'
import { Drawer } from '@/components/ui'
import { useAsync } from '@/hooks/useAsync'

import styles from './similarity.module.css'

const POSITIONS = ['1B', '2B', '3B', 'SS', 'LF', 'CF', 'RF']
const SEASONS = Array.from({ length: 13 }, (_, i) => 2015 + i)

interface Subject {
  id: number
  season: number
}

function parsePair(raw: string | null): Subject | null {
  if (!raw) return null
  const [id, season] = raw.split('-').map(Number)
  return Number.isFinite(id) && Number.isFinite(season) ? { id, season } : null
}

// -- Pair drill --------------------------------------------------------------

function PairDrawer({
  engine,
  subject,
  comp,
  position,
  vsHand,
  onClose,
}: {
  engine: string
  subject: Subject
  comp: Subject
  position?: string
  vsHand?: string
  onClose: () => void
}): React.ReactElement {
  const pair = useAsync(
    () => fetchSimilarityPair(engine, { id: subject.id, season: subject.season }, { id: comp.id, season: comp.season }, { position, vsHand }),
    [engine, subject.id, subject.season, comp.id, comp.season, position, vsHand],
  )

  return (
    <Drawer open onClose={onClose} title="Comparison detail" aria-label="Comparison detail">
      {pair.kind === 'loading' && <LoadingBlock />}
      {pair.kind === 'error' && <ErrorBlock message={pair.message} />}
      {pair.kind === 'ok' && (
        <>
          <div className={styles.pairHead}>
            <span className={styles.subjectName}>{String(pair.data.subject.name)}</span> vs{' '}
            <span className={styles.subjectName}>{pair.data.comp.name}</span> ({pair.data.comp.season})
          </div>
          {pair.data.sub_scores_meta.length >= 3 && (
            <RadarChart
              ariaLabel="Component sub-score radar"
              axes={pair.data.sub_scores_meta.map((m) => ({
                label: m.label,
                value: pair.data.comp.sub_scores[m.field] ?? 0,
              }))}
            />
          )}
          <div style={{ marginTop: 'var(--sim-space-3)' }}>
            <ComponentScoreBars
              meta={pair.data.sub_scores_meta}
              subScores={pair.data.comp.sub_scores}
              composite={pair.data.comp.score}
              sample={pair.data.comp.sample}
              minSample={undefined}
              note={pair.data.note}
            />
          </div>
          <p className={styles.callout}>
            The composite ({pair.data.comp.score.toFixed(3)}) is not the weighted sum of these bars: every engine
            applies a √(min Empirical-Bayes weight) confidence discount, and the batter engine also applies a
            bats-mismatch penalty. Read the bars as per-dimension similarity, and the sample size as the confidence.
          </p>
        </>
      )}
    </Drawer>
  )
}

// -- Results (mounted only once a subject is chosen) -------------------------

function EngineResults({
  engine,
  subject,
  position,
  vsHand,
  onOpenPair,
}: {
  engine: string
  subject: Subject
  position?: string
  vsHand?: string
  onOpenPair: (m: SimMember) => void
}): React.ReactElement {
  const q = useAsync(
    () => fetchSimilarity(engine, { entityId: subject.id, season: subject.season, position, vsHand, topN: 50, bins: 20 }),
    [engine, subject.id, subject.season, position, vsHand],
  )

  if (q.kind === 'loading') return <LoadingBlock label="Scoring the population…" />
  if (q.kind === 'error') {
    if (q.status === 503) return <EngineWarmingState engine={engine} />
    if (q.status === 404)
      return (
        <EmptyState
          title="No profile for this subject"
          hint="This player-season isn't in the engine — it may be below the minimum sample or not ingested. Try another season."
        />
      )
    return <ErrorBlock message={q.message} />
  }

  const d = q.data
  if (d.population_size === 0) {
    return <EmptyState title="No comparable seasons" hint="The population for this partition is empty." />
  }

  const summary = d.score_summary
  return (
    <>
      <div className={styles.subjectMeta}>
        <span>
          Subject: <span className={styles.subjectName}>{String(d.subject.name)}</span>
        </span>
        {Object.entries(d.subject)
          .filter(([k]) => !['entity_id', 'season', 'name'].includes(k))
          .map(([k, v]) => (
            <span key={k}>
              {k}: {String(v)}
            </span>
          ))}
        <span>{d.population_size.toLocaleString()} comps scored</span>
      </div>

      <div className={styles.twoCol}>
        <div className={styles.panel}>
          <h3 className={styles.sectionTitle}>Score distribution across the population</h3>
          <ColumnChart
            ariaLabel="Distribution of similarity scores"
            xLabel="similarity score"
            xDomain={[0, 1]}
            data={d.bins.map((b) => ({ x: (b.lo + b.hi) / 2, value: b.count }))}
          />
          <p className={styles.popNote}>
            median {summary.median?.toFixed(3)} · p75 {summary.p75?.toFixed(3)} · max {summary.max?.toFixed(3)} ·
            diagnostic {String((d.diagnostic as { status?: string }).status ?? '—')}
          </p>
        </div>
        <div className={styles.panel}>
          <h3 className={styles.sectionTitle}>How to read this engine</h3>
          <p className={styles.popNote} style={{ fontSize: 'var(--sim-text-sm)' }}>
            {d.method}. {d.partition}.
          </p>
          <p className={styles.popNote}>{d.note}</p>
          <p className={styles.popNote}>
            Each comp below shows its per-dimension sub-scores; click a row to open the full breakdown. The
            composite is discounted by sample confidence — the sample column explains any gap.
          </p>
        </div>
      </div>

      <h3 className={styles.sectionTitle}>Most similar ({d.top_n.length})</h3>
      <CompTable
        members={d.top_n}
        meta={d.sub_scores_meta}
        sampleField={d.sample_field}
        minSample={d.min_sample}
        onRowClick={onOpenPair}
      />
    </>
  )
}

// -- Page --------------------------------------------------------------------

export function EngineExplorerPage(): React.ReactElement {
  const { engine = 'pitcher' } = useParams()
  const [sp, setSp] = useSearchParams()

  const positionKeyed = engine === 'fielder'
  const supportsVsHand = engine === 'batter'
  const position = positionKeyed ? sp.get('pos') || '2B' : undefined
  const vsHand = supportsVsHand ? sp.get('vs') || undefined : undefined
  const subject = parsePair(sp.get('subject'))
  const compare = parsePair(sp.get('compare'))

  const [pendingSeason, setPendingSeason] = React.useState(2024)

  const patch = (mut: (n: URLSearchParams) => void): void => {
    const n = new URLSearchParams(sp)
    mut(n)
    setSp(n)
  }

  const selectPlayer = (id: number): void =>
    patch((n) => {
      n.set('subject', `${id}-${subject?.season ?? pendingSeason}`)
      n.delete('compare')
    })

  const setSeason = (s: number): void => {
    setPendingSeason(s)
    if (subject) patch((n) => n.set('subject', `${subject.id}-${s}`))
  }

  return (
    <div className={`${styles.page} ${styles.wide}`}>
      <Link className={styles.backLink} to="/similarity">
        ← All engines
      </Link>
      <header className={styles.header}>
        <h1 className={styles.title} style={{ textTransform: 'capitalize' }}>
          {engine.replace(/_/g, ' ')} similarity
        </h1>
      </header>

      <div className={styles.subjectBar}>
        <span className={styles.controlLabel}>{engine === 'manager' ? 'Manager' : 'Player'}</span>
        <PlayerSearchInput onSelect={(p) => selectPlayer(p.player_id)} placeholder="Search a name…" />
        <span className={styles.controlLabel}>Season</span>
        <select className={styles.select} value={subject?.season ?? pendingSeason} onChange={(e) => setSeason(Number(e.target.value))} aria-label="Season">
          {SEASONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        {positionKeyed && (
          <>
            <span className={styles.controlLabel}>Position</span>
            <select className={styles.select} value={position} onChange={(e) => patch((n) => n.set('pos', e.target.value))} aria-label="Position">
              {POSITIONS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </>
        )}
        {supportsVsHand && (
          <>
            <span className={styles.controlLabel}>vs hand</span>
            <select
              className={styles.select}
              value={vsHand ?? ''}
              onChange={(e) => patch((n) => (e.target.value ? n.set('vs', e.target.value) : n.delete('vs')))}
              aria-label="Platoon split"
            >
              <option value="">Default</option>
              <option value="L">vs LHP</option>
              <option value="R">vs RHP</option>
            </select>
          </>
        )}
      </div>

      {subject ? (
        <EngineResults
          engine={engine}
          subject={subject}
          position={position}
          vsHand={vsHand}
          onOpenPair={(m) => patch((n) => n.set('compare', `${m.entity_id}-${m.season}`))}
        />
      ) : (
        <EmptyState title={`Search a ${engine.replace(/_/g, ' ')} to see comps`} hint="Pick a name and season above." />
      )}

      {subject && compare && (
        <PairDrawer
          engine={engine}
          subject={subject}
          comp={compare}
          position={position}
          vsHand={vsHand}
          onClose={() => patch((n) => n.delete('compare'))}
        />
      )}
    </div>
  )
}
