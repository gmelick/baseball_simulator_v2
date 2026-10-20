/**
 * Panel — the design system's content section. It groups related information
 * inside a Card or as a standalone block. The game page uses it for the
 * Projections and Betting sections.
 *
 * A Panel is lighter than a Card (no shadow, a subtle background tint).
 * A ``collapsible`` panel's heading opens and closes it; ``storageKey``
 * remembers the choice in the browser.
 *
 * @example
 * <Panel label="Betting" collapsible defaultOpen={false} storageKey="gamepage.betting">
 *   {bettingCard}
 * </Panel>
 */
import React, { useState } from 'react'
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
  /** The heading becomes a button that opens and closes the panel. */
  collapsible?: boolean
  /** The open state on first render (collapsible only; default true). */
  defaultOpen?: boolean
  /** Remembers the open state in this browser under this key (collapsible only). */
  storageKey?: string
}

function readOpen(key: string | undefined, fallback: boolean): boolean {
  if (!key) return fallback
  try {
    const v = window.localStorage.getItem(key)
    return v == null ? fallback : v === '1'
  } catch {
    return fallback
  }
}

export function Panel({
  label,
  showLabel = true,
  accent = 'none',
  children,
  className,
  collapsible = false,
  defaultOpen = true,
  storageKey,
}: PanelProps): React.ReactElement {
  const [open, setOpen] = useState(() => (collapsible ? readOpen(storageKey, defaultOpen) : true))
  const toggle = (): void => {
    setOpen((o) => {
      const next = !o
      if (storageKey) {
        try {
          window.localStorage.setItem(storageKey, next ? '1' : '0')
        } catch {
          // A blocked store only forgets the choice.
        }
      }
      return next
    })
  }
  const contentId = `panel-${label.replace(/\W+/g, '-').toLowerCase()}`
  const rootClass = [
    styles.panel,
    accent !== 'none' ? styles[`accent-${accent}`] : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <section className={rootClass} aria-label={label}>
      {collapsible ? (
        <h3 className={styles.label}>
          <button
            type="button"
            className={styles.toggle}
            aria-expanded={open}
            aria-controls={contentId}
            onClick={toggle}
          >
            <span className={`${styles.caret} ${open ? styles.caretOpen : ''}`} aria-hidden="true">
              ▶
            </span>
            {label}
          </button>
        </h3>
      ) : (
        showLabel && <h3 className={styles.label}>{label}</h3>
      )}
      {/* Collapsed content stays mounted, so loaded data survives a close. */}
      <div id={contentId} className={styles.content} hidden={!open}>
        {children}
      </div>
    </section>
  )
}
