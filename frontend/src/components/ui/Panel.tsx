/**
 * Panel — SIM-380 design system primitive
 *
 * A content section that groups related information inside a Card or
 * as a standalone block. Used for the per-player simulation panels on
 * the Game page, the betting edge section, and the override comparison area.
 *
 * Panels are lighter than Cards (no shadow by default, subtle bg tint).
 *
 * @example
 * <Panel label="Win Probability" accent="primary">
 *   <WinProbabilityChart data={winProb} />
 * </Panel>
 */
import React from 'react'
import styles from './Panel.module.css'

export type PanelAccent = 'none' | 'primary' | 'success' | 'danger' | 'warning' | 'info'

export interface PanelProps {
  /** Accessible label for the panel (also rendered as the section heading) */
  label: string
  /** Whether to render the label visually (default: true) */
  showLabel?: boolean
  /** Accent bar color on the left edge */
  accent?: PanelAccent
  children: React.ReactNode
  className?: string
}

export function Panel({
  label,
  showLabel = true,
  accent = 'none',
  children,
  className,
}: PanelProps): React.ReactElement {
  const rootClass = [
    styles.panel,
    accent !== 'none' ? styles[`accent-${accent}`] : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <section className={rootClass} aria-label={label}>
      {showLabel && <h3 className={styles.label}>{label}</h3>}
      <div className={styles.content}>{children}</div>
    </section>
  )
}
