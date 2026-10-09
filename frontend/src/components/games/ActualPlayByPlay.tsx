/**
 * ActualPlayByPlay.tsx — the real game's play-by-play (the game page).
 *
 * Every plate appearance of the real game, grouped by half-inning (newest
 * first for a live game, in order for a final one). Each row names the batter
 * and the pitcher, gives the result, the outs, and the score after a scoring
 * play; its pitches fold out under it. "What if from here" opens the what-if
 * panel at the start of that plate appearance.
 */
import React from 'react'

import type { RealPlay } from '@/api/games'
import { ordinal, shortName } from '@/components/slate/format'

import styles from './ActualPlayByPlay.module.css'

export interface ActualPlayByPlayProps {
  plays: RealPlay[]
  names: Record<string, string>
  awayLabel: string
  homeLabel: string
  /** Newest half first (a live game). */
  newestFirst?: boolean
  selectedAtBat?: number | null
  onWhatIf?: (atBat: number) => void
}

interface HalfGroup {
  key: string
  inning: number
  half: 'top' | 'bottom'
  plays: RealPlay[]
}

function groupByHalf(plays: RealPlay[]): HalfGroup[] {
  const groups: HalfGroup[] = []
  for (const p of plays) {
    const key = `${p.inning}-${p.half}`
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.plays.push(p)
    else groups.push({ key, inning: p.inning, half: p.half, plays: [p] })
  }
  return groups
}

export function ActualPlayByPlay({
  plays,
  names,
  awayLabel,
  homeLabel,
  newestFirst = false,
  selectedAtBat = null,
  onWhatIf,
}: ActualPlayByPlayProps): React.ReactElement {
  if (plays.length === 0) {
    return <p className={styles.empty}>No plays yet.</p>
  }
  const name = (id: number | null): string => (id == null ? '—' : shortName(names[String(id)] ?? `#${id}`))
  const groups = groupByHalf(plays)
  const ordered = newestFirst ? [...groups].reverse() : groups

  return (
    <div className={styles.list}>
      {ordered.map((g) => (
        <section key={g.key} className={styles.half} aria-label={`${g.half === 'top' ? 'Top' : 'Bottom'} ${ordinal(g.inning)}`}>
          <h4 className={styles.halfTitle}>
            {g.half === 'top' ? '▲ Top' : '▼ Bottom'} {ordinal(g.inning)}
            <span className={styles.halfTeam}>{g.half === 'top' ? awayLabel : homeLabel} batting</span>
          </h4>
          <ol className={styles.plays}>
            {(newestFirst ? [...g.plays].reverse() : g.plays).map((p) => {
              const scored =
                p.away_score_after !== p.away_score_before || p.home_score_after !== p.home_score_before
              return (
                <li
                  key={p.at_bat}
                  className={`${styles.play} ${scored ? styles.scoring : ''} ${
                    selectedAtBat === p.at_bat ? styles.selected : ''
                  }`}
                  data-testid={`real-play-${p.at_bat}`}
                >
                  <div className={styles.row}>
                    <span className={styles.matchup}>
                      <strong>{name(p.batter_id)}</strong> vs {name(p.pitcher_id)}
                    </span>
                    <span className={styles.event}>{p.event ?? (p.is_complete ? '' : 'At bat')}</span>
                    {onWhatIf && (
                      <button
                        type="button"
                        className={styles.whatIf}
                        onClick={() => onWhatIf(p.at_bat)}
                        aria-label={`What if from ${name(p.batter_id)}'s plate appearance, ${ordinal(p.inning)} inning`}
                      >
                        What if from here
                      </button>
                    )}
                  </div>
                  {p.description && <p className={styles.desc}>{p.description}</p>}
                  <div className={styles.meta}>
                    <span>
                      {p.outs_after} out{p.outs_after === 1 ? '' : 's'}
                    </span>
                    {scored && (
                      <span className={styles.score}>
                        {awayLabel} {p.away_score_after} – {homeLabel} {p.home_score_after}
                      </span>
                    )}
                    {p.pitches.length > 0 && (
                      <details className={styles.pitches}>
                        <summary>
                          {p.pitches.length} pitch{p.pitches.length === 1 ? '' : 'es'}
                        </summary>
                        <ol>
                          {p.pitches.map((pt, i) => (
                            <li key={i}>
                              {pt.call ?? '—'}
                              {pt.type ? ` · ${pt.type}` : ''}
                              {pt.speed != null ? ` · ${pt.speed.toFixed(1)} mph` : ''}
                            </li>
                          ))}
                        </ol>
                      </details>
                    )}
                  </div>
                </li>
              )
            })}
          </ol>
        </section>
      ))}
    </div>
  )
}
