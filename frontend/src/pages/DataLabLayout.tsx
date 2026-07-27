/**
 * DataLabLayout.tsx — SIM-439
 * The Data Lab shell: a persistent segmented tab control (Summary · SQL Console
 * · AI Assistant) over a nested <Outlet/>. /lab redirects to /lab/summary.
 */
import React from 'react'
import { NavLink, Outlet } from 'react-router-dom'

import styles from './dataLab.module.css'

const TABS = [
  { to: '/lab/summary', label: 'Summary' },
  { to: '/lab/console', label: 'SQL Console' },
  { to: '/lab/assistant', label: 'AI Assistant' },
]

export function DataLabLayout(): React.ReactElement {
  return (
    <div className={styles.labShell}>
      <div className={styles.labHeader}>
        <h1 className={styles.labTitle}>Data Lab</h1>
        <p className={styles.labSubtitle}>Explore the raw pitch-by-pitch Statcast data behind the platform.</p>
        <div className={styles.tabs} role="tablist" aria-label="Data Lab views">
          {TABS.map((t) => (
            <NavLink
              key={t.to}
              to={t.to}
              role="tab"
              className={({ isActive }) => `${styles.tab} ${isActive ? styles.tabActive : ''}`}
            >
              {t.label}
            </NavLink>
          ))}
        </div>
      </div>
      <Outlet />
    </div>
  )
}
