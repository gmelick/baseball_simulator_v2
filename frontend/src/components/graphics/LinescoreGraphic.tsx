/**
 * LinescoreGraphic.tsx — SIM-392
 *
 * The classic baseball linescore: a per-inning run grid plus each team's
 * Runs / Hits / Errors totals. Renders the away row on top, home on the
 * bottom (scoreboard convention).
 *
 * Cell semantics (mirrors simulation.linescore / LinescoreModel, SIM-362):
 *   - a number     → runs scored in that half-inning
 *   - `null` ("x") → a half that was never played (bottom of the 9th when the
 *                    home team already leads, a walk-off, or innings the away
 *                    team played but the home team didn't reach). Rendered as
 *                    the classic scoreboard "x".
 *
 * Extra innings (N > 9) widen the grid automatically. Presentational only —
 * the Game page (SIM-393) adapts the /linescore payload into these props.
 */
import React from 'react'

import styles from './LinescoreGraphic.module.css'

export interface LinescoreTeam {
  /** Display name or abbreviation. */
  name: string
  /** Per-inning runs; `null` marks a half-inning that was not played ("x"). */
  byInning: Array<number | null>
  runs: number
  hits: number
  errors: number
}

export interface LinescoreGraphicProps {
  away: LinescoreTeam
  home: LinescoreTeam
  /** Optional caption for screen readers (e.g. "Final linescore"). */
  caption?: string
}

function cell(value: number | null | undefined): string {
  // undefined → blank (inning not reached by this team at all);
  // null → "x" (half-inning intentionally not played).
  if (value === undefined) return ''
  if (value === null) return 'x'
  return String(value)
}

export function LinescoreGraphic({
  away,
  home,
  caption,
}: LinescoreGraphicProps): React.ReactElement {
  const nInnings = Math.max(9, away.byInning.length, home.byInning.length)
  const inningCols = Array.from({ length: nInnings }, (_, i) => i + 1)

  return (
    <div className={styles.wrap}>
      <table className={styles.table}>
        {caption != null && <caption className={styles.caption}>{caption}</caption>}
        <thead>
          <tr>
            <th scope="col" className={styles.teamCol}>
              {/* empty corner */}
            </th>
            {inningCols.map((n) => (
              <th scope="col" key={n} className={styles.inningCol}>
                {n}
              </th>
            ))}
            <th scope="col" className={styles.totalCol}>
              R
            </th>
            <th scope="col" className={styles.totalCol}>
              H
            </th>
            <th scope="col" className={styles.totalCol}>
              E
            </th>
          </tr>
        </thead>
        <tbody>
          {[away, home].map((team, idx) => (
            <tr key={idx}>
              <th scope="row" className={styles.teamCol}>
                {team.name}
              </th>
              {inningCols.map((n) => (
                <td key={n} className={styles.inningCell}>
                  {cell(team.byInning[n - 1])}
                </td>
              ))}
              <td className={`${styles.totalCell} ${styles.runs}`}>{team.runs}</td>
              <td className={styles.totalCell}>{team.hits}</td>
              <td className={styles.totalCell}>{team.errors}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
