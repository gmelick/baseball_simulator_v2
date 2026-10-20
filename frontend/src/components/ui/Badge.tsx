/**
 * Badge — the design system's compact status indicator. It covers:
 *   - the game status: "LIVE" (pulsing), "FINAL", "SCHEDULED"
 *   - a betting signal: "+EV", a settled bet's result
 *   - a caution state: "read-only", "warming" (amber)
 *   - general counts and labels
 *
 * @example
 * <Badge variant="live" pulse>LIVE</Badge>
 * <Badge variant="success">+EV</Badge>
 * <Badge variant="warning">read-only</Badge>
 */
import React from 'react'
import styles from './Badge.module.css'

export type BadgeVariant =
  | 'default'
  | 'primary'
  | 'success'
  | 'danger'
  | 'warning'
  | 'info'
  | 'live'     // the live tag: pale red, always with the pulsing dot
  | 'final'    // the table band, ink text
  | 'scheduled' // grass green

export interface BadgeProps {
  variant?: BadgeVariant
  /** Adds a pulsing dot before the label (the LIVE status) */
  pulse?: boolean
  children: React.ReactNode
  className?: string
  /** Extra aria-label when the visual text alone is not descriptive */
  'aria-label'?: string
}

export function Badge({
  variant = 'default',
  pulse = false,
  children,
  className,
  'aria-label': ariaLabel,
}: BadgeProps): React.ReactElement {
  const rootClass = [
    styles.badge,
    styles[`variant-${variant}`],
    pulse || variant === 'live' ? styles['with-pulse'] : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <span className={rootClass} aria-label={ariaLabel}>
      {(pulse || variant === 'live') && (
        <span className={styles.dot} aria-hidden="true" />
      )}
      {children}
    </span>
  )
}
