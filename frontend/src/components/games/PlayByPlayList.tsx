/**
 * PlayByPlayList.tsx — SIM-393
 *
 * The pitch-by-pitch scroll, grouped into plate appearances. Each PA shows its
 * resolved event (carried on the terminal pitch) plus runs/outs; clicking a PA
 * expands its pitch sequence (the drill-down). A plain scroll container handles
 * a full game (~80 PAs / ~300 pitches) comfortably; swap in windowing later if
 * a much larger feed ever lands here.
 */
import React, { useMemo, useState } from 'react'

import type { PlayByPlayEntry } from '@/api/games'

import styles from './PlayByPlayList.module.css'

export interface PlayByPlayListProps {
  entries: PlayByPlayEntry[]
}

interface PlateAppearance {
  atBat: number
  pitches: PlayByPlayEntry[]
  event: string | null
  runsScored: number
  outsRecorded: number
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
      const terminal = sorted.find((p) => p.is_pa_end) ?? sorted[sorted.length - 1]
      return {
        atBat,
        pitches: sorted,
        event: terminal?.event ?? null,
        runsScored: sorted.reduce((n, p) => n + p.runs_scored, 0),
        outsRecorded: sorted.reduce((n, p) => n + p.outs_recorded, 0),
      }
    })
}

function prettyOutcome(outcome: string): string {
  return outcome.replace(/_/g, ' ')
}

export function PlayByPlayList({ entries }: PlayByPlayListProps): React.ReactElement {
  const pas = useMemo(() => groupByPa(entries), [entries])
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  if (pas.length === 0) {
    return <p className={styles.empty}>No play-by-play yet — run a simulation to populate it.</p>
  }

  const toggle = (atBat: number): void => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(atBat)) next.delete(atBat)
      else next.add(atBat)
      return next
    })
  }

  return (
    <ol className={styles.scroll} aria-label="Play-by-play">
      {pas.map((pa) => {
        const isOpen = expanded.has(pa.atBat)
        return (
          <li key={pa.atBat} className={styles.pa}>
            <button
              type="button"
              className={styles.paHeader}
              onClick={() => toggle(pa.atBat)}
              aria-expanded={isOpen}
            >
              <span className={styles.paNum}>PA {pa.atBat + 1}</span>
              <span className={styles.paEvent}>
                {pa.event != null ? prettyOutcome(pa.event) : 'in progress'}
              </span>
              <span className={styles.paMeta}>
                {pa.runsScored > 0 && (
                  <span className={styles.runs}>+{pa.runsScored} R</span>
                )}
                {pa.outsRecorded > 0 && (
                  <span className={styles.outs}>{pa.outsRecorded} out</span>
                )}
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
      })}
    </ol>
  )
}
