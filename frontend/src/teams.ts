/**
 * teams.ts — the 30 MLB clubs by MLB team id (SIM-519 Part T).
 *
 * The Daily Diamond card shows each club's logo and a chip in its primary
 * colour. The API sends the team id, name and abbreviation; this table adds
 * the colour and the bundled logo. The logos are fetched once by
 * `scripts/fetch-logos.mjs` into `public/logos/{id}.svg`, so the page calls no
 * outside host. An id not in the table gets the ink colour and no logo.
 */

export interface TeamStyle {
  abbr: string
  color: string
}

export const TEAMS: Readonly<Record<number, TeamStyle>> = {
  108: { abbr: 'LAA', color: '#BA0021' },
  109: { abbr: 'ARI', color: '#A71930' },
  110: { abbr: 'BAL', color: '#DF4601' },
  111: { abbr: 'BOS', color: '#BD3039' },
  112: { abbr: 'CHC', color: '#0E3386' },
  113: { abbr: 'CIN', color: '#C6011F' },
  114: { abbr: 'CLE', color: '#00385D' },
  115: { abbr: 'COL', color: '#333366' },
  116: { abbr: 'DET', color: '#0C2340' },
  117: { abbr: 'HOU', color: '#EB6E1F' },
  118: { abbr: 'KC', color: '#004687' },
  119: { abbr: 'LAD', color: '#005A9C' },
  120: { abbr: 'WSH', color: '#AB0003' },
  121: { abbr: 'NYM', color: '#002D72' },
  133: { abbr: 'ATH', color: '#003831' },
  134: { abbr: 'PIT', color: '#27251F' },
  135: { abbr: 'SD', color: '#2F241D' },
  136: { abbr: 'SEA', color: '#005C5C' },
  137: { abbr: 'SF', color: '#FD5A1E' },
  138: { abbr: 'STL', color: '#C41E3A' },
  139: { abbr: 'TB', color: '#092C5C' },
  140: { abbr: 'TEX', color: '#003278' },
  141: { abbr: 'TOR', color: '#134A8E' },
  142: { abbr: 'MIN', color: '#002B5C' },
  143: { abbr: 'PHI', color: '#E81828' },
  144: { abbr: 'ATL', color: '#CE1141' },
  145: { abbr: 'CWS', color: '#27251F' },
  146: { abbr: 'MIA', color: '#00A3E0' },
  147: { abbr: 'NYY', color: '#0C2340' },
  158: { abbr: 'MIL', color: '#12284B' },
}

/** The ink colour of the design, for a club the table does not know. */
export const FALLBACK_TEAM_COLOR = '#1f2a24'

export function teamColor(teamId: number | null | undefined): string {
  return (teamId != null && TEAMS[teamId]?.color) || FALLBACK_TEAM_COLOR
}

/** The bundled logo path, or null for a club the table does not know. */
export function teamLogo(teamId: number | null | undefined): string | null {
  return teamId != null && TEAMS[teamId] ? `/logos/${teamId}.svg` : null
}
