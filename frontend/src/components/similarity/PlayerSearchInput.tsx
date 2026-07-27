/**
 * PlayerSearchInput.tsx — SIM-439
 * Debounced name type-ahead against /api/players/search (raw.players pg_trgm).
 * A proper combobox/listbox: arrow/enter/escape keyboard nav, aria-activedescendant.
 * The single most-reused control across the Data Lab + Similarity Explorer.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react'

import { searchPlayers, type PlayerHit } from '@/api/players'

import styles from './PlayerSearchInput.module.css'

export interface PlayerSearchInputProps {
  onSelect: (player: PlayerHit) => void
  placeholder?: string
  /** Initial text shown in the box (e.g. a currently-selected player's name). */
  initialText?: string
  autoFocus?: boolean
}

export function PlayerSearchInput({
  onSelect,
  placeholder = 'Search a player by name…',
  initialText = '',
  autoFocus = false,
}: PlayerSearchInputProps): React.ReactElement {
  const [text, setText] = useState(initialText)
  const [hits, setHits] = useState<PlayerHit[]>([])
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [loading, setLoading] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setText(initialText)
  }, [initialText])

  // Debounced search.
  useEffect(() => {
    const q = text.trim()
    if (q.length < 2) {
      setHits([])
      setOpen(false)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    const t = window.setTimeout(() => {
      searchPlayers(q, 20, controller.signal)
        .then((res) => {
          setHits(res)
          setActive(0)
          setOpen(true)
        })
        .catch(() => {
          /* aborted or failed — leave the list as-is */
        })
        .finally(() => setLoading(false))
    }, 250)
    return () => {
      controller.abort()
      window.clearTimeout(t)
    }
  }, [text])

  // Close on outside click.
  useEffect(() => {
    const onDoc = (e: MouseEvent): void => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  const choose = useCallback(
    (p: PlayerHit) => {
      onSelect(p)
      setText(p.name)
      setOpen(false)
    },
    [onSelect],
  )

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (!open || hits.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => (a + 1) % hits.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => (a - 1 + hits.length) % hits.length)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const hit = hits[active]
      if (hit) choose(hit)
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div className={styles.box} ref={boxRef}>
      <input
        type="text"
        className={styles.input}
        value={text}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        onFocus={() => hits.length > 0 && setOpen(true)}
        role="combobox"
        aria-expanded={open}
        aria-controls="player-search-listbox"
        aria-activedescendant={open && hits[active] ? `player-opt-${hits[active].player_id}` : undefined}
        autoComplete="off"
        autoFocus={autoFocus}
      />
      {loading && <span className={styles.loading} aria-hidden="true" />}
      {open && hits.length > 0 && (
        <ul className={styles.listbox} id="player-search-listbox" role="listbox">
          {hits.map((h, i) => (
            <li
              key={h.player_id}
              id={`player-opt-${h.player_id}`}
              role="option"
              aria-selected={i === active}
              className={`${styles.option} ${i === active ? styles.optionActive : ''}`}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault()
                choose(h)
              }}
            >
              <span className={styles.optName}>{h.name}</span>
              <span className={styles.optMeta}>
                {h.position ?? ''} {h.bats ? `B:${h.bats}` : ''} {h.throws ? `T:${h.throws}` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
