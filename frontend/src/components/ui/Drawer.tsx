/**
 * Drawer — SIM-439 design system primitive
 *
 * A right-edge slide-over panel (a full-width bottom sheet on mobile), used for
 * the similarity pair-drill detail. Focus-trap-lite: closes on Esc and on
 * backdrop click, restores focus on unmount, and locks body scroll while open.
 */
import React, { useEffect, useRef } from 'react'

import styles from './Drawer.module.css'

export interface DrawerProps {
  open: boolean
  onClose: () => void
  title?: React.ReactNode
  children: React.ReactNode
  /** Accessible label when there is no visible title. */
  'aria-label'?: string
}

export function Drawer({ open, onClose, title, children, 'aria-label': ariaLabel }: DrawerProps): React.ReactElement | null {
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const prevFocus = document.activeElement as HTMLElement | null
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    panelRef.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
      prevFocus?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div
        ref={panelRef}
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={styles.header}>
          <div className={styles.title}>{title}</div>
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className={styles.body}>{children}</div>
      </div>
    </div>
  )
}
