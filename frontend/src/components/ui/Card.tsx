/**
 * Card — SIM-380 design system primitive
 *
 * A surface container with an optional title and header slot.
 * Used for game cards, player panels, betting cards, and any bounded
 * content region on the Day Summary and Game pages.
 *
 * @example
 * <Card title="Today's Games" count={12}>
 *   <GameCardList games={games} />
 * </Card>
 */
import React from 'react'
import styles from './Card.module.css'

export interface CardProps {
  /** Optional header title text */
  title?: React.ReactNode
  /** Optional count badge shown next to the title (e.g. "12 games") */
  count?: number
  /** Optional slot for action controls in the card header */
  headerActions?: React.ReactNode
  /** Card body content */
  children: React.ReactNode
  /** Extra CSS class on the root element */
  className?: string
  /** Padding variant. 'sm' for compact cards (game list rows). Default: 'md'. */
  padding?: 'sm' | 'md' | 'lg'
  /** Remove the border (useful when nesting inside a Panel) */
  borderless?: boolean
}

export function Card({
  title,
  count,
  headerActions,
  children,
  className,
  padding = 'md',
  borderless = false,
}: CardProps): React.ReactElement {
  const rootClass = [
    styles.card,
    styles[`padding-${padding}`],
    borderless ? styles['card--borderless'] : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className={rootClass}>
      {(title != null || headerActions != null) && (
        <div className={styles.header}>
          {title != null && (
            <h2 className={styles.title}>
              {title}
              {count != null && (
                <span className={styles.count} aria-label={`${count} items`}>
                  {count}
                </span>
              )}
            </h2>
          )}
          {headerActions != null && (
            <div className={styles.actions}>{headerActions}</div>
          )}
        </div>
      )}
      <div className={styles.body}>{children}</div>
    </div>
  )
}
