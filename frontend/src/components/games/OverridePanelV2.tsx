/**
 * OverridePanelV2.tsx — SIM-398 (managerial override, v2 staged queue)
 *
 * Builds on the v1 single-sub form (SIM-397) with a staged queue: add several
 * substitutions, undo any of them, then run them all at once via the SIM-388
 * `substitutions[]` array body. An amber "override active" indicator shows
 * while the queue is non-empty, and the result renders the baseline-vs-override
 * comparison side by side (OverrideDeltaView).
 */
import React, { useState } from 'react'

import {
  postWithOverride,
  type SubstitutionSlot,
  type WithOverrideResponse,
} from '@/api/games'
import { Badge } from '@/components/ui'

import { OverrideDeltaView } from './OverrideDeltaView'
import styles from './OverridePanel.module.css'

export interface OverridePanelV2Props {
  gamePk: number
}

export function OverridePanelV2({ gamePk }: OverridePanelV2Props): React.ReactElement {
  const [side, setSide] = useState<'home' | 'away'>('home')
  const [battingOrder, setBattingOrder] = useState(1)
  const [playerId, setPlayerId] = useState('')

  const [queue, setQueue] = useState<SubstitutionSlot[]>([])
  const [result, setResult] = useState<WithOverrideResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const pid = Number(playerId)
  const canAdd = Number.isInteger(pid) && pid > 0

  const addToQueue = (e: React.FormEvent): void => {
    e.preventDefault()
    if (!canAdd) return
    setQueue((q) => [...q, { side, batting_order: battingOrder, player_id: pid }])
    setPlayerId('')
  }

  const removeAt = (idx: number): void => {
    setQueue((q) => q.filter((_, i) => i !== idx))
  }

  const clearQueue = (): void => {
    setQueue([])
    setResult(null)
  }

  const runOverride = (): void => {
    if (queue.length === 0 || loading) return
    setLoading(true)
    setError(null)
    postWithOverride(
      gamePk,
      {
        substitutions: queue,
        description: `${queue.length} staged substitution${queue.length === 1 ? '' : 's'}`,
      },
      { nIterations: 100 },
    )
      .then((r) => setResult(r))
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : 'Override sim failed.'),
      )
      .finally(() => setLoading(false))
  }

  return (
    <div className={styles.panel}>
      {/* Add-to-queue form */}
      <form className={styles.form} onSubmit={addToQueue}>
        <label className={styles.field}>
          Side
          <select
            className={styles.select}
            value={side}
            onChange={(e) => setSide(e.target.value as 'home' | 'away')}
          >
            <option value="home">Home</option>
            <option value="away">Away</option>
          </select>
        </label>

        <label className={styles.field}>
          Lineup slot
          <select
            className={styles.select}
            value={battingOrder}
            onChange={(e) => setBattingOrder(Number(e.target.value))}
          >
            {Array.from({ length: 9 }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>

        <label className={styles.field}>
          Substitute (player_id)
          <input
            type="number"
            className={styles.input}
            value={playerId}
            onChange={(e) => setPlayerId(e.target.value)}
            placeholder="e.g. 660271"
            min={1}
          />
        </label>

        <button type="submit" className={styles.secondary} disabled={!canAdd}>
          + Stage
        </button>
      </form>

      {/* Staged queue */}
      {queue.length > 0 && (
        <>
          <div className={styles.queueHeader}>
            <Badge variant="warning">Override active · {queue.length} staged</Badge>
            <div className={styles.queueActions}>
              <button type="button" className={styles.secondary} onClick={clearQueue} disabled={loading}>
                Clear all
              </button>
              <button type="button" className={styles.submit} onClick={runOverride} disabled={loading}>
                {loading ? 'Running…' : `Run ${queue.length} change${queue.length === 1 ? '' : 's'}`}
              </button>
            </div>
          </div>

          <ul className={styles.queue}>
            {queue.map((s, i) => (
              <li key={`${s.side}-${s.batting_order}-${s.player_id}-${i}`} className={styles.queueItem}>
                <span className={styles.queueText}>
                  {s.side} slot {s.batting_order} → player {s.player_id}
                </span>
                <button
                  type="button"
                  className={styles.removeButton}
                  onClick={() => removeAt(i)}
                  aria-label={`Undo ${s.side} slot ${s.batting_order} substitution`}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {error && <p className={styles.error} role="alert">{error}</p>}

      {result && (
        <div className={styles.result}>
          <OverrideDeltaView delta={result.delta} />
        </div>
      )}
    </div>
  )
}
