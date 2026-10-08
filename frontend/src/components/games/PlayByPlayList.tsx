/**
 * PlayByPlayList.tsx — SIM-393, SIM-561
 *
 * The pitch-by-pitch scroll, grouped into plate appearances. Each PA shows its
 * resolved event (carried on the terminal pitch) plus runs/outs; clicking a PA
 * expands its pitch sequence (the drill-down). A plain scroll container handles
 * a full game (~80 PAs / ~300 pitches) comfortably; swap in windowing later if
 * a much larger feed ever lands here.
 *
 * SIM-561: a stored game carries each pitch's inning, half, batter, pitcher and
 * score. The scroll groups the plate appearances under a header per half-inning,
 * names the batter and the pitcher, and shows the score after a scoring play. A
 * stream stored without them reads as before ("PA 12").
 */
import React, { useMemo, useState } from 'react'

import type { PlayByPlayEntry } from '@/api/games'

import styles from './PlayByPlayList.module.css'

export interface PlayByPlayListProps {
  entries: PlayByPlayEntry[]
  /** str(player_id) → name (the /plays response's `names`). */
  names?: Record<string, string>
  /** The away team's short label for the half headers and the score. */
  awayLabel?: string
  /** The home team's short label. */
  homeLabel?: string
  /** The text shown when there are no plays. */
  emptyText?: string
}

interface PlateAppearance {
  atBat: number
  pitches: PlayByPlayEntry[]
  event: string | null
  runsScored: number
  outsRecorded: number
  inning: number | null
  half: 'top' | 'bottom' | null
  batterId: number | null
  pitcherId: number | null
  awayScore: number | null
  homeScore: number | null
}

interface HalfInning {
  key: string
  inning: number | null
  half: 'top' | 'bottom' | null
  pas: PlateAppearance[]
}

function groupByPa(entries: PlayByPlayEntry[]): PlateAppearance[] {
  const byPa = new Map<number, PlayByPlayEntry[]>()
  for (const e of entries) {
    const arr = byPa.get(e.at_bat)
    if (arr) arr.push(e)
    else byPa.set(e.at_bat, [e])
  }
  return Array.from(byPa.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([atBat, pitches]) => {
      const sorted = [...pitches].sort((a, b) => a.pitch - b.pitch)
      const first = sorted[0]
      const terminal = sorted.find((p) => p.is_pa_end) ?? sorted[sorted.length - 1]
      return {
        atBat,
        pitches: sorted,
        event: terminal?.event ?? null,
        runsScored: sorted.reduce((n, p) => n + p.runs_scored, 0),
        outsRecorded: sorted.reduce((n, p) => n + p.outs_recorded, 0),
        inning: first?.inning ?? null,
        half: first?.half ?? null,
        batterId: first?.batter_id ?? null,
        // The arm on the mound when the plate appearance ended.
        pitcherId: terminal?.pitcher_id ?? null,
        awayScore: terminal?.away_score ?? null,
        homeScore: terminal?.home_score ?? null,
      }
    })
}

/** Consecutive plate appearances of one half-inning, in game order. */
function groupByHalf(pas: PlateAppearance[]): HalfInning[] {
  const halves: HalfInning[] = []
  for (const pa of pas) {
    const key = `${pa.inning ?? '?'}-${pa.half ?? '?'}`
    const last = halves[halves.length - 1]
    if (last && last.key === key) last.pas.push(pa)
    else halves.push({ key, inning: pa.inning, half: pa.half, pas: [pa] })
  }
  return halves
}

function ordinal(n: number): string {
  const tens = n % 100
  if (tens >= 11 && tens <= 13) return `${n}th`
  return `${n}${['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'}`
}

function prettyOutcome(outcome: string): string {
  return outcome.replace(/_/g, ' ')
}

export function PlayByPlayList({
  entries,
  names = {},
  awayLabel = 'Away',
  homeLabel = 'Home',
  emptyText = 'No play-by-play yet — run a simulation to populate it.',
}: PlayByPlayListProps): React.ReactElement {
  const halves = useMemo(() => groupByHalf(groupByPa(entries)), [entries])
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  if (halves.length === 0) {
    return <p className={styles.empty}>{emptyText}</p>
  }

  const nameOf = (id: number | null): string | null =>
    id == null ? null : (names[String(id)] ?? `Player ${id}`)

  const toggle = (atBat: number): void => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(atBat)) next.delete(atBat)
      else next.add(atBat)
      return next
    })
  }

  const renderPa = (pa: PlateAppearance): React.ReactElement => {
    const isOpen = expanded.has(pa.atBat)
    const batter = nameOf(pa.batterId)
    const pitcher = nameOf(pa.pitcherId)
    return (
      <li key={pa.atBat} className={styles.pa}>
        <button
          type="button"
          className={styles.paHeader}
          onClick={() => toggle(pa.atBat)}
          aria-expanded={isOpen}
        >
          {batter == null && <span className={styles.paNum}>PA {pa.atBat + 1}</span>}
          <span className={styles.paMain}>
            {batter != null && (
              <span className={styles.matchup}>
                <span className={styles.batter}>{batter}</span>
                {pitcher != null && <span className={styles.vs}> vs {pitcher}</span>}
              </span>
            )}
            <span className={styles.paEvent}>
              {pa.event != null ? prettyOutcome(pa.event) : 'in progress'}
            </span>
          </span>
          <span className={styles.paMeta}>
            {pa.runsScored > 0 && <span className={styles.runs}>+{pa.runsScored} R</span>}
            {pa.runsScored > 0 && pa.awayScore != null && pa.homeScore != null && (
              <span className={styles.score}>
                {awayLabel} {pa.awayScore}, {homeLabel} {pa.homeScore}
              </span>
            )}
            {pa.outsRecorded > 0 && <span className={styles.outs}>{pa.outsRecorded} out</span>}
            <span className={styles.caret} aria-hidden="true">
              {isOpen ? '▾' : '▸'}
            </span>
          </span>
        </button>
        {isOpen && (
          <ul className={styles.pitches}>
            {pa.pitches.map((p) => (
              <li key={p.sequence} className={styles.pitch}>
                <span className={styles.pitchNum}>#{p.pitch}</span>
                <span className={styles.pitchOutcome}>{prettyOutcome(p.pitch_outcome)}</span>
                {p.is_contact && p.exit_velo != null && (
                  <span className={styles.bip}>
                    {Math.round(p.exit_velo)} mph
                    {p.launch_angle != null ? `, ${Math.round(p.launch_angle)}°` : ''}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </li>
    )
  }

  return (
    <ol className={styles.scroll} aria-label="Play-by-play">
      {halves.map((h) =>
        h.inning == null || h.half == null ? (
          h.pas.map(renderPa)
        ) : (
          <li key={h.key} className={styles.half}>
            <h3 className={styles.halfHeader}>
              {h.half === 'top' ? 'Top' : 'Bottom'} {ordinal(h.inning)}
              <span className={styles.halfTeam}>
                {h.half === 'top' ? awayLabel : homeLabel} batting
              </span>
            </h3>
            <ol className={styles.halfList}>{h.pas.map(renderPa)}</ol>
          </li>
        ),
      )}
    </ol>
  )
}
