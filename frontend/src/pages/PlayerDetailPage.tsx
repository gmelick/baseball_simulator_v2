/**
 * PlayerDetailPage.tsx — SIM-439
 * Cross-engine hub for one player: identity + seasons on file, and a card per
 * relevant similarity engine that deep-links into its explorer pre-seeded with
 * this player + their latest season. Reachable from any player-id cell/comp row.
 */
import React from 'react'
import { Link, useParams } from 'react-router-dom'

import { fetchPlayer, type PlayerDetail } from '@/api/players'
import { KpiRow, StatTile } from '@/components/lab/StatTile'
import { ErrorBlock, LoadingBlock } from '@/components/lab/states'
import { PlayerSearchInput } from '@/components/similarity/PlayerSearchInput'
import { Badge } from '@/components/ui'
import { useAsync } from '@/hooks/useAsync'

import styles from './similarity.module.css'

const FIELDER_POS = new Set(['1B', '2B', '3B', 'SS', 'LF', 'CF', 'RF'])

function coverageEngines(p: PlayerDetail, season: number): { engine: string; label: string; to: string }[] {
  const out: { engine: string; label: string; to: string }[] = []
  const seed = (engine: string, extra = ''): string => `/similarity/${engine}?subject=${p.player_id}-${season}${extra}`
  const isPitcher = p.throws != null && (p.position === 'P' || p.position == null)

  if (isPitcher) {
    out.push({ engine: 'pitcher', label: 'Pitcher (arsenal + command)', to: seed('pitcher') })
    out.push({ engine: 'pitcher_steal', label: 'Pitcher (runner suppression)', to: seed('pitcher_steal') })
  }
  out.push({ engine: 'batter', label: 'Batter (offense)', to: seed('batter') })
  out.push({ engine: 'baserunner', label: 'Baserunner (extra bases)', to: seed('baserunner') })
  out.push({ engine: 'baserunner_steal', label: 'Baserunner (steals)', to: seed('baserunner_steal') })
  if (p.position === 'C') out.push({ engine: 'catcher', label: 'Catcher (framing/blocking)', to: seed('catcher') })
  if (p.position && FIELDER_POS.has(p.position))
    out.push({ engine: 'fielder', label: `Fielder (${p.position})`, to: seed('fielder', `&pos=${p.position}`) })
  return out
}

export function PlayerDetailPage(): React.ReactElement {
  const { playerId } = useParams()
  const id = Number(playerId)
  const player = useAsync(() => fetchPlayer(id), [id])

  return (
    <div className={styles.page}>
      <div style={{ marginBottom: 'var(--sim-space-3)', maxWidth: 320 }}>
        <PlayerSearchInput onSelect={(p) => (window.location.href = `/player/${p.player_id}`)} placeholder="Jump to another player…" />
      </div>

      {player.kind === 'loading' && <LoadingBlock />}
      {player.kind === 'error' && <ErrorBlock message={player.message} />}
      {player.kind === 'ok' && (
        <>
          <header className={styles.header}>
            <h1 className={styles.title}>{player.data.name}</h1>
            <div className={styles.subjectMeta}>
              {player.data.position && <Badge variant="default">{player.data.position}</Badge>}
              {player.data.bats && <span>Bats: {player.data.bats}</span>}
              {player.data.throws && <span>Throws: {player.data.throws}</span>}
              <span>ID {player.data.player_id}</span>
            </div>
          </header>

          <KpiRow>
            <StatTile
              label="Seasons on file"
              value={player.data.seasons.length}
              context={`${player.data.seasons[0] ?? '—'}–${player.data.seasons[player.data.seasons.length - 1] ?? '—'}`}
            />
          </KpiRow>

          <h2 className={styles.groupTitle}>Explore similarity</h2>
          {player.data.seasons.length === 0 ? (
            <p className={styles.popNote}>No pitch data on file for this player, so no similarity profiles.</p>
          ) : (
            <div className={styles.engineGrid}>
              {coverageEngines(player.data, player.data.seasons[player.data.seasons.length - 1] ?? 2024).map((c) => (
                <Link key={c.engine} to={c.to} className={styles.engineCard}>
                  <div className={styles.engineName} style={{ textTransform: 'none' }}>
                    {c.label}
                  </div>
                  <div className={styles.engineMeta}>Explore comps →</div>
                </Link>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
