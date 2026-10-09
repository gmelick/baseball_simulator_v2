/**
 * BoxscorePanel.tsx — SIM-394, SIM-560
 *
 * Per-player projections: the 100-iteration boxscore means (SIM-366) plus, on
 * demand, the full prop distribution (SIM-390) with a prop-line marker.
 *
 * The boxscore endpoint runs an N-iteration sim, so it's gated behind an
 * explicit "Load projections" button rather than firing on mount. Once loaded,
 * each player's prop means render as chips; clicking a chip fetches that prop's
 * PMF and shows the distribution chart, with an optional line input that
 * redraws the over/under marker.
 *
 * SIM-560: the panel picks one seed per load and sends it with every request,
 * so each click reads the cached run the chips show. The players are grouped by
 * team: batters in batting order, then pitchers, starter first.
 *
 * SIM-561: the API stores the run's first game for the linescore and the
 * play-by-play; `onLoaded` tells the page, which reloads both.
 *
 * SIM-519 Part E: the panel reads the game's stored run (`runId`, from the
 * Simulation card) and runs no batch of its own; each click reads the same run.
 */
import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  fetchBoxscore,
  fetchPropEdge,
  type BoxscoreCard,
  type BoxscoreRow,
  type PropEdge,
} from '@/api/games'

import { PropDistributionChart } from './PropDistributionChart'
import styles from './BoxscorePanel.module.css'

/** The games the panel simulates per load. */
const N_GAMES = 100

/** The pitcher props (simulation/prop_distributions.py PITCHER_PROPS). */
const PITCHER_PROPS = new Set(['K', 'BB', 'ER', 'OUTS', 'H_ALLOWED'])

/** Display labels for prop codes that read poorly as-is. */
const PROP_LABELS: Record<string, string> = {
  H_ALLOWED: 'H',
  OUTS: 'Outs',
  HRR: 'H+R+RBI',
}

function propLabel(prop: string): string {
  return PROP_LABELS[prop] ?? prop
}

function playerName(row: BoxscoreRow): string {
  return row.name ?? `Player ${row.player_id}`
}

function newSeed(): number {
  return Math.floor(Math.random() * 1_000_000_000)
}

export interface BoxscorePanelProps {
  gamePk: number
  /** The away team's display name (falls back to "Away"). */
  awayLabel?: string
  /** The home team's display name (falls back to "Home"). */
  homeLabel?: string
  /** Called once the card loads; the API stored the run's first game (SIM-561). */
  onLoaded?: () => void
  /** SIM-519 Part E: the stored run to read; null shows the "run a simulation" prompt. */
  runId?: number | null
}

interface Selection {
  playerId: number
  prop: string
}

interface PlayerLine {
  row: BoxscoreRow
  /** The props this line shows: the batting props or the pitching props. */
  props: [string, number][]
  /** The slot number for a batter, "SP" / "RP" for a pitcher. */
  marker: string
}

interface TeamGroup {
  key: string
  label: string
  batters: PlayerLine[]
  pitchers: PlayerLine[]
}

/** Split the card into team groups: batters by slot, then pitchers, starter first. */
function groupByTeam(box: BoxscoreCard, awayLabel: string, homeLabel: string): TeamGroup[] {
  const groups: TeamGroup[] = [
    { key: 'away', label: awayLabel, batters: [], pitchers: [] },
    { key: 'home', label: homeLabel, batters: [], pitchers: [] },
    { key: 'other', label: 'Other players', batters: [], pitchers: [] },
  ]
  const bySide = (side: BoxscoreRow['side']): TeamGroup =>
    side === 'away' ? groups[0] : side === 'home' ? groups[1] : groups[2]

  for (const row of Object.values(box.players)) {
    const entries = Object.entries(row.means)
    const batting = entries.filter(([prop]) => !PITCHER_PROPS.has(prop))
    const pitching = entries.filter(([prop]) => PITCHER_PROPS.has(prop))
    const group = bySide(row.side)
    if (batting.length > 0) {
      const slot = row.lineup_slot
      group.batters.push({ row, props: batting, marker: slot != null ? String(slot) : '–' })
    }
    if (pitching.length > 0) {
      group.pitchers.push({
        row,
        props: pitching,
        marker: row.synthetic ? 'BP' : row.starting_pitcher ? 'SP' : 'RP',
      })
    }
  }

  for (const g of groups) {
    g.batters.sort(
      (a, b) =>
        (a.row.lineup_slot ?? 99) - (b.row.lineup_slot ?? 99) || a.row.player_id - b.row.player_id,
    )
    // Starter first, then the relievers by the outs they get, most first.
    g.pitchers.sort(
      (a, b) =>
        Number(b.row.starting_pitcher ?? false) - Number(a.row.starting_pitcher ?? false) ||
        (b.row.means.OUTS ?? 0) - (a.row.means.OUTS ?? 0) ||
        a.row.player_id - b.row.player_id,
    )
  }
  return groups.filter((g) => g.batters.length > 0 || g.pitchers.length > 0)
}

export function BoxscorePanel({
  gamePk,
  awayLabel = 'Away',
  homeLabel = 'Home',
  onLoaded,
  runId = null,
}: BoxscorePanelProps): React.ReactElement {
  const [box, setBox] = useState<BoxscoreCard | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [selection, setSelection] = useState<Selection | null>(null)
  const [lineInput, setLineInput] = useState('')
  const [dist, setDist] = useState<PropEdge | null>(null)
  const [distLoading, setDistLoading] = useState(false)

  const loadBoxscore = (): void => {
    setLoading(true)
    setError(null)
    fetchBoxscore(gamePk, N_GAMES, newSeed())
      .then((b) => {
        setBox(b)
        onLoaded?.()
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : 'Failed to load projections.'),
      )
      .finally(() => setLoading(false))
  }

  // SIM-519 Part E: a new stored run replaces the card.
  useEffect(() => {
    if (runId == null) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setSelection(null)
    fetchBoxscore(gamePk, undefined, undefined, runId)
      .then((b) => !cancelled && setBox(b))
      .catch((err: unknown) => !cancelled && setError(err instanceof Error ? err.message : 'Failed to load projections.'))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [gamePk, runId])

  // Fetch the selected prop's distribution (re-runs when the line changes). The
  // card's seed and N make the server read the cached run, not simulate again.
  const runSeed = box?.base_seed ?? undefined
  const runGames = box?.n_iterations
  useEffect(() => {
    if (!selection) {
      setDist(null)
      return
    }
    let cancelled = false
    setDistLoading(true)
    const lineNum = lineInput.trim() === '' ? undefined : Number(lineInput)
    const line = lineNum != null && Number.isFinite(lineNum) ? lineNum : undefined
    fetchPropEdge(gamePk, selection.playerId, selection.prop, {
      line,
      baseSeed: runSeed,
      nIterations: runGames,
      runId,
    })
      .then((d) => !cancelled && setDist(d))
      .catch(() => !cancelled && setDist(null))
      .finally(() => !cancelled && setDistLoading(false))
    return () => {
      cancelled = true
    }
  }, [gamePk, selection, lineInput, runSeed, runGames, runId])

  if (!box) {
    if (runId != null || loading) {
      return (
        <div className={styles.gate}>
          <p aria-busy={loading}>{loading ? 'Loading the run…' : error ?? ''}</p>
        </div>
      )
    }
    return (
      <div className={styles.gate}>
        <p>Run a simulation above to see each player's projections.</p>
        <button type="button" className={styles.loadButton} onClick={loadBoxscore} disabled={loading}>
          Quick look ({N_GAMES} games, not stored)
        </button>
        {error && <p className={styles.error} role="alert">{error}</p>}
      </div>
    )
  }

  const groups = groupByTeam(box, awayLabel, homeLabel)
  const selectedRow = selection ? box.players[String(selection.playerId)] : undefined

  const renderLine = (line: PlayerLine): React.ReactElement => (
    <li key={`${line.row.player_id}-${line.marker}`} className={styles.playerRow}>
      <span className={styles.marker}>{line.marker}</span>
      <span className={styles.playerName}>
        {line.row.player_id > 0 ? (
          <Link to={`/player/${line.row.player_id}`}>{playerName(line.row)}</Link>
        ) : (
          playerName(line.row)
        )}
      </span>
      <span className={styles.chips}>
        {line.props.map(([prop, mean]) => {
          if (line.row.synthetic) {
            // The summed pen has no single distribution to open.
            return (
              <span key={prop} className={styles.chip}>
                <span className={styles.chipProp}>{propLabel(prop)}</span>
                <span className={styles.chipMean}>{mean.toFixed(2)}</span>
              </span>
            )
          }
          const active = selection?.playerId === line.row.player_id && selection?.prop === prop
          return (
            <button
              key={prop}
              type="button"
              className={`${styles.chip} ${active ? styles.chipActive : ''}`}
              onClick={() => setSelection(active ? null : { playerId: line.row.player_id, prop })}
              aria-pressed={active}
              title={prop}
            >
              <span className={styles.chipProp}>{propLabel(prop)}</span>
              <span className={styles.chipMean}>{mean.toFixed(2)}</span>
            </button>
          )
        })}
      </span>
    </li>
  )

  return (
    <div className={styles.panel}>
      <p className={styles.caption}>
        Averages over {box.n_iterations} simulated games. Click a stat to see its distribution.
      </p>

      {groups.map((g) => (
        <section key={g.key} className={styles.team} aria-label={g.label}>
          <h3 className={styles.teamName}>{g.label}</h3>
          {g.batters.length > 0 && (
            <>
              <h4 className={styles.groupLabel}>Batting</h4>
              <ul className={styles.playerList}>{g.batters.map(renderLine)}</ul>
            </>
          )}
          {g.pitchers.length > 0 && (
            <>
              <h4 className={styles.groupLabel}>Pitching</h4>
              <ul className={styles.playerList}>{g.pitchers.map(renderLine)}</ul>
              {g.pitchers.some((l) => l.row.synthetic) && (
                <p className={styles.caption}>
                  This game&apos;s bullpen is not listed yet; the relievers are a generic pen, shown as one row.
                </p>
              )}
            </>
          )}
        </section>
      ))}

      {selection && (
        <div className={styles.detail}>
          <div className={styles.detailHeader}>
            <strong>
              {dist?.player_name ?? (selectedRow ? playerName(selectedRow) : `Player ${selection.playerId}`)} —{' '}
              {propLabel(selection.prop)}
            </strong>
            <label className={styles.lineLabel}>
              Line
              <input
                type="number"
                step="0.5"
                className={styles.lineInput}
                value={lineInput}
                onChange={(e) => setLineInput(e.target.value)}
                placeholder="e.g. 5.5"
              />
            </label>
          </div>

          {distLoading && <p className={styles.muted}>Loading distribution…</p>}

          {!distLoading && dist && (
            <>
              <PropDistributionChart
                support={dist.support}
                probabilities={dist.probabilities}
                line={dist.line}
                mean={dist.mean}
                propLabel={propLabel(dist.prop)}
              />
              <div className={styles.stats}>
                <span>mean {dist.mean.toFixed(2)}</span>
                <span>median {dist.median.toFixed(1)}</span>
                <span>sd {dist.std.toFixed(2)}</span>
                {dist.p_over != null && <span>P(over) {(dist.p_over * 100).toFixed(0)}%</span>}
                {dist.p_under != null && <span>P(under) {(dist.p_under * 100).toFixed(0)}%</span>}
                {dist.p_push != null && dist.p_push > 0 && (
                  <span>P(push) {(dist.p_push * 100).toFixed(0)}%</span>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
