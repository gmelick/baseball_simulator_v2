/**
 * OverridePanelV1.tsx — SIM-397 (managerial override, v1 single-sub)
 *
 * The simplest override: swap ONE lineup slot and re-run the matchup against
 * the baseline at the same seed, then show the metric deltas. A single form
 * (side, batting order 1–9, substitute player_id, optional note) POSTs to
 * /simulate/with_override and renders the OverrideDeltaView. The richer staged
 * queue (multi-change + undo) is SIM-398.
 */
import React, { useState } from 'react'

import {
  postWithOverride,
  type WithOverrideResponse,
} from '@/api/games'

import { OverrideDeltaView } from './OverrideDeltaView'
import styles from './OverridePanel.module.css'

export interface OverridePanelV1Props {
  gamePk: number
}

export function OverridePanelV1({ gamePk }: OverridePanelV1Props): React.ReactElement {
  const [side, setSide] = useState<'home' | 'away'>('home')
  const [battingOrder, setBattingOrder] = useState(1)
  const [playerId, setPlayerId] = useState('')
  const [note, setNote] = useState('')

  const [result, setResult] = useState<WithOverrideResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const pid = Number(playerId)
  const canSubmit = Number.isInteger(pid) && pid > 0 && !loading

  const submit = (e: React.FormEvent): void => {
    e.preventDefault()
    if (!canSubmit) return
    setLoading(true)
    setError(null)
    postWithOverride(
      gamePk,
      {
        substitutions: [{ batting_order: battingOrder, player_id: pid, side }],
        description: note.trim() || `Sub ${side} slot ${battingOrder} → ${pid}`,
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
      <form className={styles.form} onSubmit={submit}>
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

        <label className={`${styles.field} ${styles.noteField}`}>
          Note (optional)
          <input
            type="text"
            className={styles.input}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="why this move?"
          />
        </label>

        <button type="submit" className={styles.submit} disabled={!canSubmit}>
          {loading ? 'Running baseline + override…' : 'Run override'}
        </button>
      </form>

      {error && <p className={styles.error} role="alert">{error}</p>}

      {result && (
        <div className={styles.result}>
          <OverrideDeltaView delta={result.delta} />
        </div>
      )}
    </div>
  )
}
