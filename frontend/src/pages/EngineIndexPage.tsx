/**
 * EngineIndexPage.tsx — SIM-439
 * The Similarity engine chooser. Group A = the 8 player-comparison (score)
 * engines with a build-status badge; Group B = the pattern-finder (distance)
 * engines, honestly labeled and routed to the Situation Finder / Data Lab.
 */
import React from 'react'
import { Link } from 'react-router-dom'

import { fetchEngineCatalog, type EngineCatalogEntry } from '@/api/similarity'
import { ErrorBlock, LoadingBlock } from '@/components/lab/states'
import { Badge } from '@/components/ui'
import { useAsync } from '@/hooks/useAsync'

import styles from './similarity.module.css'

function BuildBadge({ e }: { e: EngineCatalogEntry }): React.ReactElement {
  if (e.kind === 'distance') return <Badge variant="info">pattern finder</Badge>
  if (e.built) return <Badge variant="success">{e.profile_count.toLocaleString()} profiles</Badge>
  return <Badge variant="warning">warming</Badge>
}

function routeFor(e: EngineCatalogEntry): string {
  if (e.engine === 'situation') return '/similarity/situation'
  if (e.kind === 'distance') return '/lab/summary'
  return `/similarity/${e.engine}`
}

export function EngineIndexPage(): React.ReactElement {
  const catalog = useAsync(() => fetchEngineCatalog(), [])

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>Similarity Explorer</h1>
        <p className={styles.subtitle}>
          Every engine scores a player-season (or game state) against the whole population. The 8 score engines
          expose the component sub-scores behind each composite; the pattern finders return a distance, not a comp.
        </p>
      </header>

      {catalog.kind === 'loading' && <LoadingBlock />}
      {catalog.kind === 'error' && <ErrorBlock message={catalog.message} />}
      {catalog.kind === 'ok' && (
        <>
          <h2 className={styles.groupTitle}>Player comparisons</h2>
          <div className={styles.engineGrid}>
            {catalog.data
              .filter((e) => e.kind === 'score')
              .map((e) => (
                <Link
                  key={e.engine}
                  to={routeFor(e)}
                  className={`${styles.engineCard} ${e.built ? '' : styles.engineDim}`}
                >
                  <div className={styles.engineTop}>
                    <span className={styles.engineName}>{e.engine.replace(/_/g, ' ')}</span>
                    <BuildBadge e={e} />
                  </div>
                  <div className={styles.engineMethod}>{e.method}</div>
                  <div className={styles.engineMeta}>
                    {e.sub_score_count} sub-score{e.sub_score_count === 1 ? '' : 's'} · {e.partition}
                  </div>
                </Link>
              ))}
          </div>

          <h2 className={styles.groupTitle}>Pattern finders</h2>
          <div className={styles.engineGrid}>
            {catalog.data
              .filter((e) => e.kind === 'distance')
              .map((e) => (
                <Link key={e.engine} to={routeFor(e)} className={styles.engineCard}>
                  <div className={styles.engineTop}>
                    <span className={styles.engineName}>{e.engine.replace(/_/g, ' ')}</span>
                    <BuildBadge e={e} />
                  </div>
                  <div className={styles.engineMethod}>{e.method}</div>
                </Link>
              ))}
          </div>
        </>
      )}
    </div>
  )
}
