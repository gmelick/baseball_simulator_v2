/**
 * TeamBlock.tsx — one club on a Daily Diamond card: the logo, the colour chip
 * with the abbreviation, the name and the record (SIM-519). The away club
 * aligns left, the home club right; a losing club dims, as in the design.
 */
import React, { useState } from 'react'

import { teamColor, teamLogo } from '@/teams'

import styles from './TeamBlock.module.css'

export interface TeamBlockProps {
  side: 'away' | 'home'
  teamId: number | null
  abbr: string | null
  name: string
  wins: number | null
  losses: number | null
  dim?: boolean
}

export function TeamBlock({ side, teamId, abbr, name, wins, losses, dim = false }: TeamBlockProps): React.ReactElement {
  const logo = teamLogo(teamId)
  const [logoOk, setLogoOk] = useState(true)
  return (
    <div className={`${styles.team} ${side === 'home' ? styles.home : ''} ${dim ? styles.dim : ''}`}>
      {logo && logoOk ? (
        <img className={styles.logo} src={logo} alt="" onError={() => setLogoOk(false)} />
      ) : (
        <div className={styles.logo} aria-hidden="true" />
      )}
      <div className={styles.chip} style={{ background: teamColor(teamId) }}>
        {abbr ?? '—'}
      </div>
      <div className={styles.name}>{name}</div>
      {wins != null && losses != null && (
        <div className={styles.record}>
          {wins}-{losses}
        </div>
      )}
    </div>
  )
}
