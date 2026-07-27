/**
 * SummaryPage.tsx — SIM-439
 * Descriptive dashboard over raw.pitches. A KPI strip + a season/handedness/
 * pitcher filter bar drive a responsive grid of cards, each independently
 * loaded (one failing card never blanks the page) and each with an
 * "Open in Console" deep-link to its underlying SQL.
 */
import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  fetchBySeason,
  fetchCountState,
  fetchFreshness,
  fetchKpis,
  fetchPaOutcomes,
  fetchPitchTypeMix,
  fetchVeloDistribution,
  fetchZone,
} from '@/api/lab'
import type { PlayerHit } from '@/api/players'
import { BarList, ColumnChart, HeatGrid } from '@/components/charts'
import { DataFreshnessBadge, KpiRow, StatTile } from '@/components/lab/StatTile'
import { ErrorBlock, LoadingBlock } from '@/components/lab/states'
import { PlayerSearchInput } from '@/components/similarity/PlayerSearchInput'
import { Card } from '@/components/ui'
import { type AsyncState, useAsync } from '@/hooks/useAsync'

import styles from './dataLab.module.css'

function ChartCard<T>({
  title,
  note,
  sql,
  state,
  render,
}: {
  title: string
  note?: string
  sql?: string
  state: AsyncState<T>
  render: (d: T) => React.ReactNode
}): React.ReactElement {
  const navigate = useNavigate()
  return (
    <Card>
      <div className={styles.cardHead}>
        <h3 className={styles.cardTitle}>{title}</h3>
        {sql && (
          <button className={styles.cardAction} onClick={() => navigate('/lab/console', { state: { sql } })}>
            Open in Console
          </button>
        )}
      </div>
      <div className={styles.cardBody}>
        {state.kind === 'loading' && <LoadingBlock />}
        {state.kind === 'error' && <ErrorBlock message={state.message} />}
        {state.kind === 'ok' && render(state.data)}
      </div>
      {note && <p className={styles.cardNote}>{note}</p>}
    </Card>
  )
}

function SummaryCards({
  season,
  pitcher,
  stand,
}: {
  season: number
  pitcher: number | null
  stand: string | null
}): React.ReactElement {
  const bySeason = useAsync(() => fetchBySeason(), [])
  const mix = useAsync(() => fetchPitchTypeMix({ season, pitcher, stand }), [season, pitcher, stand])
  const outcomes = useAsync(() => fetchPaOutcomes({ season }), [season])
  const counts = useAsync(() => fetchCountState({ season, pitcher }), [season, pitcher])
  const velo = useAsync(() => fetchVeloDistribution({ season, pitcher }), [season, pitcher])
  const zone = useAsync(() => fetchZone({ season, pitcher }), [season, pitcher])

  return (
    <div className={styles.grid}>
      <ChartCard
        title="Pitches by season"
        state={bySeason}
        sql="SELECT season, COUNT(DISTINCT game_pk) AS games, COUNT(*) AS pitches FROM raw.pitches GROUP BY season ORDER BY season;"
        render={(rows) => (
          <BarList
            ariaLabel="Pitches by season"
            items={rows.map((r) => ({ label: String(r.season), value: r.pitches, sub: `${r.games.toLocaleString()} G` }))}
            formatValue={(v) => v.toLocaleString()}
          />
        )}
      />

      <ChartCard
        title={`Pitch-type mix${pitcher ? '' : ' (league)'} · ${season}`}
        note="Usage% with average velo per pitch — the shape the arsenal engine models."
        state={mix}
        sql={`SELECT pitch_type, COUNT(*) n, ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (),1) usage_pct, ROUND(AVG(release_speed)::numeric,1) avg_velo FROM raw.pitches WHERE season=${season}${pitcher ? ` AND pitcher=${pitcher}` : ''}${stand ? ` AND stand='${stand}'` : ''} AND data_quality_flag=FALSE GROUP BY pitch_type ORDER BY n DESC;`}
        render={(rows) =>
          rows.length === 0 ? (
            <p className={styles.message}>No pitches match this filter.</p>
          ) : (
            <BarList
              ariaLabel="Pitch-type usage"
              items={rows.map((r) => ({
                label: r.pitch_type,
                value: r.usage_pct ?? r.n,
                sub: r.avg_velo != null ? `${r.avg_velo} mph` : undefined,
              }))}
              formatValue={(v) => `${v.toFixed(1)}%`}
            />
          )
        }
      />

      <ChartCard
        title={`Plate-appearance outcomes · ${season}`}
        state={outcomes}
        sql={`SELECT events AS event, COUNT(*) n FROM raw.pitches WHERE season=${season} AND events IS NOT NULL AND data_quality_flag=FALSE GROUP BY events ORDER BY n DESC;`}
        render={(rows) => (
          <BarList
            ariaLabel="PA outcomes"
            maxItems={12}
            items={rows.map((r) => ({ label: r.event, value: r.n }))}
            formatValue={(v) => v.toLocaleString()}
          />
        )}
      />

      <ChartCard
        title={`Whiff% by count · ${season}`}
        note="Rows = balls, columns = strikes. Darker = higher whiff rate."
        state={counts}
        sql={`SELECT balls, strikes, COUNT(*) pitches FROM raw.pitches WHERE season=${season}${pitcher ? ` AND pitcher=${pitcher}` : ''} AND data_quality_flag=FALSE GROUP BY balls, strikes ORDER BY balls, strikes;`}
        render={(rows) => (
          <HeatGrid
            rows={4}
            cols={3}
            rowLabels={['0', '1', '2', '3']}
            colLabels={['0 str', '1 str', '2 str']}
            ariaLabel="Whiff rate by ball-strike count"
            cell={(r, c) => {
              const row = rows.find((x) => x.balls === r && x.strikes === c)
              if (!row) return { value: null, label: '—' }
              return { value: row.whiff_pct ?? 0, label: row.whiff_pct != null ? `${row.whiff_pct}%` : '—', title: `${r}-${c}: ${row.pitches} pitches` }
            }}
          />
        )}
      />

      <ChartCard
        title={`Release-velo distribution · ${season}`}
        state={velo}
        sql={`SELECT FLOOR(release_speed)::int velo, COUNT(*) n FROM raw.pitches WHERE season=${season}${pitcher ? ` AND pitcher=${pitcher}` : ''} AND release_speed IS NOT NULL AND data_quality_flag=FALSE GROUP BY 1 ORDER BY 1;`}
        render={(rows) =>
          rows.length === 0 ? (
            <p className={styles.message}>No velocity data.</p>
          ) : (
            <ColumnChart ariaLabel="Release-velo distribution" xLabel="mph" data={rows.map((r) => ({ x: r.velo_bucket, value: r.n }))} />
          )
        }
      />

      <ChartCard
        title={`Whiff% by zone · ${season}`}
        note="Statcast zones 1–9 (catcher's view). Darker = higher whiff rate."
        state={zone}
        sql={`SELECT zone, COUNT(*) pitches FROM raw.pitches WHERE season=${season}${pitcher ? ` AND pitcher=${pitcher}` : ''} AND zone IS NOT NULL AND data_quality_flag=FALSE GROUP BY zone ORDER BY zone;`}
        render={(rows) => (
          <HeatGrid
            rows={3}
            cols={3}
            ariaLabel="Whiff rate by strike-zone bucket"
            cell={(r, c) => {
              const z = r * 3 + c + 1
              const row = rows.find((x) => x.zone === z)
              if (!row) return { value: null, label: '—' }
              return { value: row.whiff_pct ?? 0, label: row.whiff_pct != null ? `${row.whiff_pct}%` : '—', title: `zone ${z}: ${row.pitches} pitches` }
            }}
          />
        )}
      />
    </div>
  )
}

export function SummaryPage(): React.ReactElement {
  const kpis = useAsync(() => fetchKpis(), [])
  const freshness = useAsync(() => fetchFreshness(), [])

  const [season, setSeason] = useState<number | undefined>(undefined)
  const [pitcher, setPitcher] = useState<PlayerHit | null>(null)
  const [stand, setStand] = useState<string>('')

  const seasons = useMemo(() => (kpis.kind === 'ok' ? kpis.data.seasons : []), [kpis])

  useEffect(() => {
    if (season === undefined && seasons.length > 0) setSeason(seasons[seasons.length - 1])
  }, [seasons, season])

  return (
    <div>
      <div className={styles.kpiWrap}>
        <div className={styles.kpiHeader}>
          <h2 className={styles.cardTitle}>Coverage</h2>
          {freshness.kind === 'ok' && <DataFreshnessBadge date={freshness.data.latest_game_date} />}
        </div>
        {kpis.kind === 'loading' && <LoadingBlock />}
        {kpis.kind === 'error' && <ErrorBlock message={kpis.message} />}
        {kpis.kind === 'ok' && (
          <KpiRow>
            <StatTile
              label="Pitches"
              value={`≈${(kpis.data.total_pitches / 1e6).toFixed(1)}M`}
              context="estimated"
              title={`${kpis.data.total_pitches.toLocaleString()} (pg_class estimate)`}
            />
            <StatTile label="Games" value={kpis.data.total_games.toLocaleString()} />
            <StatTile
              label="Seasons"
              value={kpis.data.seasons.length}
              context={`${kpis.data.seasons[0] ?? ''}–${kpis.data.seasons[kpis.data.seasons.length - 1] ?? ''}`}
            />
            <StatTile label="First game" value={kpis.data.first_date ?? '—'} />
            <StatTile label="Latest game" value={kpis.data.last_date ?? '—'} />
          </KpiRow>
        )}
      </div>

      <div className={styles.filterBar}>
        <span className={styles.filterLabel}>Season</span>
        <select className={styles.select} value={season ?? ''} onChange={(e) => setSeason(Number(e.target.value))} aria-label="Season">
          {seasons.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <span className={styles.filterLabel}>Pitcher</span>
        <PlayerSearchInput
          placeholder="All pitchers — search to scope…"
          initialText={pitcher?.name ?? ''}
          onSelect={(p) => setPitcher(p)}
        />
        {pitcher && (
          <button className={styles.cardAction} onClick={() => setPitcher(null)}>
            clear
          </button>
        )}
        <span className={styles.filterLabel}>vs batter side</span>
        <select className={styles.select} value={stand} onChange={(e) => setStand(e.target.value)} aria-label="Batter side">
          <option value="">Both</option>
          <option value="L">L</option>
          <option value="R">R</option>
        </select>
      </div>

      {season !== undefined ? (
        <SummaryCards season={season} pitcher={pitcher?.player_id ?? null} stand={stand || null} />
      ) : (
        kpis.kind === 'ok' && <p className={styles.message}>No seasons ingested yet.</p>
      )}
    </div>
  )
}
