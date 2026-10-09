/**
 * DatePicker.tsx — the design's date control (SIM-519): ‹ and › step a day,
 * the date button opens a month calendar, and "Today" shows when the slate is
 * on another day. Today's cell carries a grass ring; the chosen day is forest.
 */
import React, { useEffect, useRef, useState } from 'react'

import { parseIso, shiftIso, shortDateLabel, todayIso, toIso } from './format'
import styles from './DatePicker.module.css'

export interface DatePickerProps {
  date: string
  onChange: (iso: string) => void
  wide: boolean
}

const WEEKDAYS = ['S', 'M', 'T', 'W', 'T', 'F', 'S']

export function DatePicker({ date, onChange, wide }: DatePickerProps): React.ReactElement {
  const [open, setOpen] = useState(false)
  const [month, setMonth] = useState<[number, number] | null>(null)
  const today = todayIso()
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const d = parseIso(date)
  const [y, m] = month ?? [d.getFullYear(), d.getMonth()]
  const first = new Date(y, m, 1)
  const days = new Date(y, m + 1, 0).getDate()
  const cells: Array<string | null> = [
    ...Array.from({ length: first.getDay() }, () => null),
    ...Array.from({ length: days }, (_, i) => toIso(new Date(y, m, i + 1))),
  ]
  const shiftMonth = (n: number): void => {
    const x = new Date(y, m + n, 1)
    setMonth([x.getFullYear(), x.getMonth()])
  }
  const pick = (iso: string): void => {
    setOpen(false)
    onChange(iso)
  }

  return (
    <nav className={styles.nav} aria-label="Date navigation" ref={ref}>
      <button type="button" className={styles.step} onClick={() => onChange(shiftIso(date, -1))} aria-label="Previous day">
        ‹
      </button>
      <button
        type="button"
        className={styles.dateButton}
        onClick={() => {
          setMonth(null)
          setOpen((o) => !o)
        }}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={`Choose a date, ${shortDateLabel(date, true)}`}
      >
        <span>{shortDateLabel(date, wide)}</span>
        <span className={styles.caret} aria-hidden="true">
          ▼
        </span>
      </button>
      <button type="button" className={styles.step} onClick={() => onChange(shiftIso(date, 1))} aria-label="Next day">
        ›
      </button>
      {date !== today && (
        <button type="button" className={styles.today} onClick={() => onChange(today)}>
          Today
        </button>
      )}

      {open && (
        <>
          <div className={styles.scrim} onClick={() => setOpen(false)} aria-hidden="true" />
          <div className={styles.popup} role="dialog" aria-label="Calendar">
            <div className={styles.monthRow}>
              <button type="button" className={styles.monthStep} onClick={() => shiftMonth(-1)} aria-label="Previous month">
                ‹
              </button>
              <div className={styles.monthTitle}>
                {first.toLocaleDateString([], { month: 'long', year: 'numeric' })}
              </div>
              <button type="button" className={styles.monthStep} onClick={() => shiftMonth(1)} aria-label="Next month">
                ›
              </button>
            </div>
            <div className={styles.weekdays} aria-hidden="true">
              {WEEKDAYS.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
            <div className={styles.grid}>
              {cells.map((iso, i) =>
                iso == null ? (
                  <div key={`blank-${i}`} />
                ) : (
                  <button
                    key={iso}
                    type="button"
                    className={`${styles.day} ${iso === date ? styles.selected : ''} ${
                      iso === today && iso !== date ? styles.isToday : ''
                    }`}
                    onClick={() => pick(iso)}
                    aria-label={parseIso(iso).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })}
                    aria-current={iso === date ? 'date' : undefined}
                  >
                    {Number(iso.slice(8))}
                  </button>
                ),
              )}
            </div>
          </div>
        </>
      )}
    </nav>
  )
}
