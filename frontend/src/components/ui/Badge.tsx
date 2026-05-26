/**
 * Badge — SIM-380 design system primitive
 *
 * A compact status indicator. Covers:
 *   - Game status: "LIVE" (pulsing), "FINAL", "SCHEDULED"
 *   - Betting signal: "+EV", "CLV +"
 *   - Override state: "OVERRIDE ACTIVE" (amber)
 *   - General counts / labels
 *
 * @example
 * <Badge variant="live" pulse>LIVE</Badge>
 * <Badge variant="success">+EV</Badge>
 * <Badge variant="warning">OVERRIDE</Badge>
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
  | 'live'     // shorthand for info + pulse
  | 'final'    // shorthand for muted/gray
  | 'scheduled' // shorthand for gray outline

export interface BadgeProps {
  variant?: BadgeVariant
  /** Adds a pulsing dot before the label (used for LIVE status) */
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
