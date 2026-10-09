/**
 * WhatIfPanel.tsx — go back to a plate appearance of the real game, make
 * managerial changes, and simulate the rest of the game from there.
 *
 * The panel reads the game as it stood at the start of the chosen plate
 * appearance (GET /feed/state) and draws it on the slate's field. The manager
 * stages changes — a pinch hitter, a reliever, a pinch runner, a defensive
 * change (before first pitch: either side's lineup and starter) — then runs
 * 100 games twice from that spot with the same seeds: as it really stood and
 * with the changes. The result compares the two beside the real final.
 */
import React, { useEffect, useMemo, useState } from 'react'

import {
  fetchPaState,
  fetchWhatIf,
  GamesApiError,
  startWhatIf,
  type PaState,
  type PlayerOption,
  type SimRun,
  type WhatIfChanges,
  type WhatIfResult,
  type WhatIfStarted,
} from '@/api/games'
import { CardField } from '@/components/slate/CardField'
import { ordinal, shortName } from '@/components/slate/format'

import styles from './WhatIfPanel.module.css'

const POSITIONS = ['C', '1B', '2B', '3B', 'SS', 'LF', 'CF', 'RF'] as const
const BASES = ['first', 'second', 'third'] as const
const POLL_MS = 2000
const N_GAMES = 100

type Side = 'away' | 'home'

interface Staged {
  key: string
  label: string
  apply: (c: WhatIfChanges) => void
  /** The bench player or arm this change uses (so no other change can). */
  uses: number
}

export interface WhatIfPanelProps {
  gamePk: number
  /** The plate appearance (the feed's atBatIndex); null = first pitch. */
  atBat: number | null
  awayAbbr: string
  homeAbbr: string
  onClose?: () => void
}

function emptyChanges(): WhatIfChanges {
  return { pinch_hit: [], pitcher: null, pinch_run: [], defense: [] }
}

function caption(s: PaState, awayAbbr: string, homeAbbr: string): string {
  if (s.at_bat == null) return 'Before first pitch'
  const half = s.half === 'top' ? 'Top' : 'Bottom'
  const batter = s.live.batter?.name ?? '—'
  const pitcher = s.live.pitcher?.name ?? '—'
  return `${half} ${ordinal(s.inning)}, ${s.outs} out${s.outs === 1 ? '' : 's'}, ${awayAbbr} ${s.away_score}–${s.home_score} ${homeAbbr}: ${shortName(batter)} vs ${shortName(pitcher)}`
}

function pct(x: number | undefined): string {
  return x == null ? '—' : `${(x * 100).toFixed(1)}%`
}

function signed(x: number, digits = 2, suffix = ''): string {
  const v = x.toFixed(digits)
  return `${x > 0 ? '+' : ''}${v}${suffix}`
}

export function WhatIfPanel({ gamePk, atBat, awayAbbr, homeAbbr, onClose }: WhatIfPanelProps): React.ReactElement {
  const [state, setState] = useState<PaState | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [staged, setStaged] = useState<Staged[]>([])
  const [started, setStarted] = useState<WhatIfStarted | null>(null)
  const [result, setResult] = useState<WhatIfResult | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // The state at the chosen plate appearance; a new choice clears the panel.
  useEffect(() => {
    let cancelled = false
    setState(null)
    setLoadError(null)
    setStaged([])
    setStarted(null)
    setResult(null)
    setRunError(null)
    fetchPaState(gamePk, atBat)
      .then((s) => !cancelled && setState(s))
      .catch((err: unknown) => !cancelled && setLoadError(err instanceof Error ? err.message : 'Failed to load the play.'))
    return () => {
      cancelled = true
    }
  }, [gamePk, atBat])

  // Poll the two runs until both finish.
  const finished = (r: SimRun | undefined): boolean =>
    r != null && (r.status === 'done' || r.status === 'failed' || r.status === 'cancelled')
  const pending = started != null && !(result && finished(result.base) && finished(result.change))
  useEffect(() => {
    if (!started || !pending) return
    let cancelled = false
    const poll = (): void => {
      fetchWhatIf(gamePk, started.base_run_id, started.change_run_id)
        .then((r) => !cancelled && setResult(r))
        .catch(() => undefined)
    }
    poll()
    const id = window.setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [gamePk, started, pending])

  const used = useMemo(() => new Set(staged.map((s) => s.uses)), [staged])
  const pregame = state?.at_bat == null
  const batting: Side = state?.batting_side ?? 'away'
  const fielding: Side = batting === 'away' ? 'home' : 'away'

  const stage = (s: Staged): void => setStaged((list) => [...list.filter((x) => x.key !== s.key), s])
  const unstage = (key: string): void => setStaged((list) => list.filter((x) => x.key !== key))

  const run = (): void => {
    const changes = emptyChanges()
    staged.forEach((s) => s.apply(changes))
    setBusy(true)
    setRunError(null)
    setResult(null)
    startWhatIf(gamePk, atBat, changes, N_GAMES)
      .then((s) => setStarted(s))
      .catch((err: unknown) =>
        setRunError(
          err instanceof GamesApiError && err.status === 422 ? `Not allowed: ${err.message}` : err instanceof Error ? err.message : 'The run could not start.',
        ),
      )
      .finally(() => setBusy(false))
  }

  if (loadError) {
    return (
      <div className={styles.panel}>
        <p className={styles.error} role="alert">
          {loadError}
        </p>
      </div>
    )
  }
  if (!state) {
    return (
      <div className={styles.panel}>
        <p className={styles.muted} aria-busy="true">
          Loading the game at that point…
        </p>
      </div>
    )
  }

  const abbr = (s: Side): string => (s === 'away' ? awayAbbr : homeAbbr)
  const sideSt = (s: Side) => state[s]
  const available = (list: PlayerOption[]): PlayerOption[] => list.filter((p) => !used.has(p.id))

  return (
    <div className={styles.panel} data-testid="what-if">
      <div className={styles.head}>
        <div>
          <div className={styles.kicker}>What if</div>
          <div className={styles.caption}>{caption(state, awayAbbr, homeAbbr)}</div>
        </div>
        {onClose && (
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close the what-if">
            ×
          </button>
        )}
      </div>

      <div className={styles.grid}>
        <div className={styles.fieldCol}>
          <CardField live={state.live} />
          <p className={styles.muted}>
            {abbr(batting)} batting · P {state.live.pitcher?.name ?? '—'} ({state.live.pitcher?.np ?? 0} pitches)
          </p>
        </div>

        <div className={styles.controls}>
          {(pregame ? (['away', 'home'] as Side[]) : [batting]).map((side) => (
            <PinchHitControl
              key={`ph-${side}`}
              side={side}
              title={pregame ? `${abbr(side)} lineup change` : 'Pinch-hit'}
              lineup={sideSt(side).lineup}
              defaultSlot={pregame ? 0 : sideSt(side).lineup.findIndex((l) => l.id === state.live.batter?.id)}
              bench={available(sideSt(side).bench)}
              onStage={(slot, player) =>
                stage({
                  key: `ph-${side}-${slot}`,
                  label: `${abbr(side)}: ${player.name} bats in slot ${slot + 1} for ${sideSt(side).lineup[slot]?.name}`,
                  uses: player.id,
                  apply: (c) => c.pinch_hit.push({ slot, player_id: player.id, ...(pregame ? { side } : {}) }),
                })
              }
            />
          ))}

          {(pregame ? (['home', 'away'] as Side[]) : [fielding]).map((side) => (
            <PickControl
              key={`p-${side}`}
              title={pregame ? `${abbr(side)} starting pitcher` : 'New pitcher'}
              label={`Replace ${sideSt(side).pitcher.name} with`}
              options={available(sideSt(side).bullpen)}
              onStage={(player) =>
                stage({
                  key: `p-${side}`,
                  label: `${abbr(side)}: ${player.name} pitches for ${sideSt(side).pitcher.name}`,
                  uses: player.id,
                  apply: (c) => {
                    c.pitcher = { player_id: player.id, ...(pregame ? { side } : {}) }
                  },
                })
              }
            />
          ))}

          {!pregame &&
            BASES.filter((b) => state.live.runners[b] != null).map((base) => (
              <PickControl
                key={`pr-${base}`}
                title={`Pinch-run at ${base}`}
                label={`For ${state.live.runners[base]?.name}`}
                options={available(sideSt(batting).bench)}
                onStage={(player) =>
                  stage({
                    key: `pr-${base}`,
                    label: `Pinch-run: ${player.name} for ${state.live.runners[base]?.name} at ${base}`,
                    uses: player.id,
                    apply: (c) => c.pinch_run.push({ base, player_id: player.id }),
                  })
                }
              />
            ))}

          {(pregame ? (['home', 'away'] as Side[]) : [fielding]).map((side) => (
            <DefenseControl
              key={`d-${side}`}
              side={side}
              title={pregame ? `${abbr(side)} defense` : 'Defensive change'}
              state={sideSt(side)}
              bench={available(sideSt(side).bench)}
              onStage={(position, player) =>
                stage({
                  key: `d-${side}-${position}`,
                  label: `${abbr(side)}: ${player.name} to ${position}`,
                  uses: player.id,
                  apply: (c) => c.defense.push({ side, position, player_id: player.id }),
                })
              }
            />
          ))}
        </div>
      </div>

      <div className={styles.staged}>
        <div className={styles.stagedHead}>
          <span>Changes ({staged.length})</span>
          <button type="button" className={styles.primary} onClick={run} disabled={busy || (started != null && pending)}>
            Simulate {N_GAMES} from here
          </button>
        </div>
        {staged.length === 0 ? (
          <p className={styles.muted}>No changes: the run compares the game as it stood with itself.</p>
        ) : (
          <ul className={styles.stagedList}>
            {staged.map((s) => (
              <li key={s.key}>
                {s.label}
                <button type="button" className={styles.unstage} onClick={() => unstage(s.key)} aria-label={`Remove: ${s.label}`}>
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
        {runError && (
          <p className={styles.error} role="alert">
            {runError}
          </p>
        )}
      </div>

      {started && <Results started={started} result={result} awayAbbr={awayAbbr} homeAbbr={homeAbbr} />}
    </div>
  )
}

function PickControl({
  title,
  label,
  options,
  onStage,
}: {
  title: string
  label: string
  options: PlayerOption[]
  onStage: (p: PlayerOption) => void
}): React.ReactElement {
  const [pick, setPick] = useState('')
  const chosen = options.find((o) => String(o.id) === pick)
  return (
    <fieldset className={styles.control}>
      <legend>{title}</legend>
      <label className={styles.row}>
        <span>{label}</span>
        <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label={`${title}: player`}>
          <option value="">Choose…</option>
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
              {o.throws ? ` (${o.throws}HP)` : o.bats ? ` (bats ${o.bats})` : ''}
            </option>
          ))}
        </select>
        <button type="button" disabled={!chosen} onClick={() => chosen && (onStage(chosen), setPick(''))}>
          Stage
        </button>
      </label>
    </fieldset>
  )
}

function PinchHitControl({
  side,
  title,
  lineup,
  defaultSlot,
  bench,
  onStage,
}: {
  side: Side
  title: string
  lineup: PaState['away']['lineup']
  defaultSlot: number
  bench: PlayerOption[]
  onStage: (slot: number, p: PlayerOption) => void
}): React.ReactElement {
  const [slot, setSlot] = useState(String(Math.max(0, defaultSlot)))
  const [pick, setPick] = useState('')
  const chosen = bench.find((o) => String(o.id) === pick)
  return (
    <fieldset className={styles.control}>
      <legend>{title}</legend>
      <label className={styles.row}>
        <span>For</span>
        <select value={slot} onChange={(e) => setSlot(e.target.value)} aria-label={`${title}: lineup slot (${side})`}>
          {lineup.map((l) => (
            <option key={l.slot} value={l.slot}>
              {l.slot + 1}. {l.name} {l.position ?? ''}
            </option>
          ))}
        </select>
      </label>
      <label className={styles.row}>
        <span>Use</span>
        <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label={`${title}: bench player (${side})`}>
          <option value="">Choose…</option>
          {bench.map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
              {o.bats ? ` (bats ${o.bats})` : ''}
            </option>
          ))}
        </select>
        <button type="button" disabled={!chosen} onClick={() => chosen && (onStage(Number(slot), chosen), setPick(''))}>
          Stage
        </button>
      </label>
    </fieldset>
  )
}

function DefenseControl({
  side,
  title,
  state,
  bench,
  onStage,
}: {
  side: Side
  title: string
  state: PaState['away']
  bench: PlayerOption[]
  onStage: (position: string, p: PlayerOption) => void
}): React.ReactElement {
  const [position, setPosition] = useState<string>('C')
  const [pick, setPick] = useState('')
  // A player already in the game (a switch) or a bench player.
  const inGame = state.lineup
    .filter((l) => l.position !== position && l.position !== 'P')
    .map((l) => ({ id: l.id, name: `${l.name} (now ${l.position ?? 'DH'})`, position: l.position, bats: null, throws: null }))
  const options: PlayerOption[] = [...inGame, ...bench]
  const chosen = options.find((o) => String(o.id) === pick)
  return (
    <fieldset className={styles.control}>
      <legend>{title}</legend>
      <label className={styles.row}>
        <span>At</span>
        <select value={position} onChange={(e) => setPosition(e.target.value)} aria-label={`${title}: position (${side})`}>
          {POSITIONS.map((p) => (
            <option key={p} value={p}>
              {p} — {state.defense[p]?.name ?? 'open'}
            </option>
          ))}
        </select>
      </label>
      <label className={styles.row}>
        <span>Put</span>
        <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label={`${title}: player (${side})`}>
          <option value="">Choose…</option>
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
            </option>
          ))}
        </select>
        <button type="button" disabled={!chosen} onClick={() => chosen && (onStage(position, chosen), setPick(''))}>
          Stage
        </button>
      </label>
    </fieldset>
  )
}

function Results({
  started,
  result,
  awayAbbr,
  homeAbbr,
}: {
  started: WhatIfStarted
  result: WhatIfResult | null
  awayAbbr: string
  homeAbbr: string
}): React.ReactElement {
  const base = result?.base
  const change = result?.change
  const progress = (r: SimRun | undefined): string =>
    r == null ? 'queued' : r.status === 'running' ? `${r.progress_done}/${r.n_iterations}` : r.status
  if (!base || !change || base.status !== 'done' || change.status !== 'done' || !base.summary || !change.summary) {
    const failed = [base, change].find((r) => r && (r.status === 'failed' || r.status === 'cancelled'))
    return (
      <div className={styles.results} role="status">
        {failed ? (
          <p className={styles.error}>The run {failed.status}{failed.error ? `: ${failed.error}` : ''}.</p>
        ) : (
          <p className={styles.muted}>
            Simulating… as it stood {progress(base)} · with the changes {progress(change)}
          </p>
        )}
      </div>
    )
  }
  const b = base.summary
  const c = change.summary
  const start = result?.start_score ?? { away: 0, home: 0 }
  const rows: Array<[string, string, string, string]> = [
    [`${homeAbbr} win`, pct(b.home_win_pct), pct(c.home_win_pct), signed((c.home_win_pct - b.home_win_pct) * 100, 1, ' pts')],
    [`${awayAbbr} win`, pct(b.away_win_pct), pct(c.away_win_pct), signed((c.away_win_pct - b.away_win_pct) * 100, 1, ' pts')],
    [
      'Expected final',
      `${b.away_score_mean.toFixed(2)}–${b.home_score_mean.toFixed(2)}`,
      `${c.away_score_mean.toFixed(2)}–${c.home_score_mean.toFixed(2)}`,
      '',
    ],
    [
      `${awayAbbr} runs from here`,
      (b.away_score_mean - start.away).toFixed(2),
      (c.away_score_mean - start.away).toFixed(2),
      signed(c.away_score_mean - b.away_score_mean),
    ],
    [
      `${homeAbbr} runs from here`,
      (b.home_score_mean - start.home).toFixed(2),
      (c.home_score_mean - start.home).toFixed(2),
      signed(c.home_score_mean - b.home_score_mean),
    ],
  ]
  return (
    <div className={styles.results}>
      {started.changes.length > 0 && <p className={styles.muted}>{started.changes.join(' · ')}</p>}
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col" />
            <th scope="col">As it stood</th>
            <th scope="col">With changes</th>
            <th scope="col">Difference</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, x, y, d]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td>{x}</td>
              <td>{y}</td>
              <td className={d.startsWith('+') ? styles.up : d.startsWith('-') ? styles.down : ''}>{d}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className={styles.footnote}>
        {base.n_iterations} games each, the same seeds.
        {result?.real_final ? ` Real final: ${awayAbbr} ${result.real_final.away}–${result.real_final.home} ${homeAbbr}.` : ''}
      </p>
    </div>
  )
}
