/**
 * CardDetail.tsx — the open Daily Diamond card (SIM-519 Part I).
 *
 * Reads the real game from GET /api/games/{pk}/feed and the book's lines from
 * GET /api/betting/games/{pk}/card-odds, then shows the design's sections by
 * state:
 *
 *   scheduled  the lineups (or the probable starters); the pregame odds
 *   live       the linescore; the field; the count, pitcher and last play; the box score
 *   final      the linescore; the box score; the pregame lines and how they settled
 *   postponed  the reason and the make-up date
 *
 * A section with no data hides itself. Every open card links to the game page.
 */
import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { type CardOdds, fetchCardOdds } from '@/api/betting'
import {
  fetchGameFeed,
  type FeedBoxSide,
  type FeedLinescore,
  type GameCard,
  type GameFeedCard,
  type GameStatus,
} from '@/api/games'

import { CardField } from './CardField'
import styles from './CardDetail.module.css'
import { american, parseIso, shortName, signedLine } from './format'

interface CardDetailProps {
  game: GameCard
  status: GameStatus
  tick: number
}

type Load<T> = { kind: 'loading' } | { kind: 'ok'; data: T } | { kind: 'none' } | { kind: 'error'; message: string }

export function CardDetail({ game, status, tick }: CardDetailProps): React.ReactElement {
  const [feed, setFeed] = useState<Load<GameFeedCard>>({ kind: 'loading' })
  const [odds, setOdds] = useState<Load<CardOdds>>({ kind: 'loading' })
  const wantsOdds = status === 'scheduled' || status === 'final'
  const wantsFeed = status !== 'postponed'

  // The feed: on open, and again on each slate refresh while the game is live.
  const feedKey = status === 'live' ? tick : 0
  useEffect(() => {
    if (!wantsFeed) return
    let cancelled = false
    fetchGameFeed(game.game_pk)
      .then((data) => !cancelled && setFeed({ kind: 'ok', data }))
      .catch((err: unknown) => {
        if (cancelled) return
        // Keep the last good copy on a refresh error.
        setFeed((prev) => (prev.kind === 'ok' ? prev : { kind: 'error', message: errorText(err) }))
      })
    return () => {
      cancelled = true
    }
  }, [game.game_pk, wantsFeed, feedKey])

  useEffect(() => {
    if (!wantsOdds) return
    let cancelled = false
    fetchCardOdds(game.game_pk)
      .then((data) => !cancelled && setOdds({ kind: 'ok', data }))
      .catch(() => !cancelled && setOdds({ kind: 'none' }))
    return () => {
      cancelled = true
    }
  }, [game.game_pk, wantsOdds])

  const f = feed.kind === 'ok' ? feed.data : null

  return (
    <>
      {status === 'postponed' && <Postponed game={game} />}

      {status === 'scheduled' && <Lineups game={game} feed={f} loading={feed.kind === 'loading'} />}

      {(status === 'live' || status === 'final') && f?.linescore && (
        <Linescore game={game} ls={f.linescore} liveOffense={f.live?.offense ?? null} />
      )}

      {status === 'live' && f?.live && <OnTheField game={game} feed={f} />}

      {(status === 'live' || status === 'final') && f?.box && <BoxScore game={game} feed={f} />}

      {feed.kind === 'loading' && status !== 'scheduled' && wantsFeed && (
        <p className={styles.note} aria-busy="true">
          Loading the game…
        </p>
      )}
      {feed.kind === 'error' && wantsFeed && (
        <p className={styles.note} role="status">
          The league feed is unavailable: {feed.message}
        </p>
      )}
      {f?.source === 'feed_cached' && <p className={styles.note}>Showing the last good copy of the league feed.</p>}

      {wantsOdds && odds.kind === 'ok' && <Odds game={game} odds={odds.data} final={status === 'final'} />}

      <div className={styles.footer}>
        <Link to={`/game/${game.game_pk}`} className={styles.openLink}>
          Open game →
        </Link>
      </div>
    </>
  )
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : 'request failed'
}

// ---------------------------------------------------------------------------
// Section frame
// ---------------------------------------------------------------------------

function Section({ title, children }: { title: string; children: React.ReactNode }): React.ReactElement {
  return (
    <section className={styles.section} aria-label={title}>
      <h3 className={styles.sectionTitle}>
        {title}
        <span className={styles.rule} aria-hidden="true" />
      </h3>
      {children}
    </section>
  )
}

const teamLabel = (game: GameCard, side: 'away' | 'home'): string => {
  const abbr = side === 'away' ? game.away_team_abbrev : game.home_team_abbrev
  const name = side === 'away' ? game.away_team_name : game.home_team_name
  return [abbr, name].filter(Boolean).join(' ') || (side === 'away' ? 'Away' : 'Home')
}

// ---------------------------------------------------------------------------
// Postponed
// ---------------------------------------------------------------------------

function Postponed({ game }: { game: GameCard }): React.ReactElement {
  const makeup = game.rescheduled_to
    ? parseIso(game.rescheduled_to).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })
    : null
  return (
    <Section title="Postponed">
      <p className={styles.text}>
        {game.detailed_state ?? 'Postponed'}
        {game.reason && !(game.detailed_state ?? '').includes(game.reason) ? ` (${game.reason})` : ''}.
        {makeup ? ` Rescheduled to ${makeup}.` : ' No make-up date yet.'}
      </p>
    </Section>
  )
}

// ---------------------------------------------------------------------------
// Lineups (preview)
// ---------------------------------------------------------------------------

function Lineups({ game, feed, loading }: { game: GameCard; feed: GameFeedCard | null; loading: boolean }): React.ReactElement {
  const posted = !!feed && feed.lineups.away.length > 0 && feed.lineups.home.length > 0
  const col = (side: 'away' | 'home'): React.ReactElement => {
    const lineup = feed?.lineups[side] ?? []
    const sp =
      (side === 'away' ? feed?.lineups.away_probable_pitcher?.name : feed?.lineups.home_probable_pitcher?.name) ??
      (side === 'away' ? game.away_probable_pitcher_name : game.home_probable_pitcher_name)
    return (
      <div className={styles.lineupCol} key={side}>
        <div className={styles.lineupTeam}>{teamLabel(game, side)}</div>
        <div className={styles.lineupSp}>
          SP <strong>{sp ? shortName(sp) : 'TBD'}</strong>
        </div>
        {lineup.length === 0 ? (
          <div className={styles.lineupEmpty}>{loading ? 'Loading…' : 'Lineup not yet posted'}</div>
        ) : (
          <ol className={styles.lineupList}>
            {lineup.map((p) => (
              <li key={p.order} className={styles.lineupRow}>
                <span className={styles.lineupN}>{p.order}</span>
                <span className={styles.lineupName}>{shortName(p.name)}</span>
                <span className={styles.lineupPos}>{p.pos}</span>
              </li>
            ))}
          </ol>
        )}
      </div>
    )
  }
  return (
    <Section title={posted ? 'Confirmed lineups' : 'Probable starters'}>
      <div className={styles.lineups}>
        {col('away')}
        {col('home')}
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------------------
// Linescore
// ---------------------------------------------------------------------------

function Linescore({
  game,
  ls,
  liveOffense,
}: {
  game: GameCard
  ls: FeedLinescore
  liveOffense: 'away' | 'home' | null
}): React.ReactElement {
  const n = Math.max(9, ls.innings.length)
  const cols = Array.from({ length: n }, (_, i) => i + 1)
  const currentIdx = ls.current_inning != null ? ls.current_inning - 1 : -1
  const row = (side: 'away' | 'home'): React.ReactElement => {
    const totals = ls[side]
    return (
      <tr key={side}>
        <th scope="row" className={styles.lsTeam}>
          {(side === 'away' ? game.away_team_abbrev : game.home_team_abbrev) ?? side}
        </th>
        {cols.map((c, i) => {
          const inn = ls.innings[i]
          let v = ''
          if (inn) {
            const runs = inn[side]
            if (runs != null) v = String(runs)
            else if (side === 'home' && i === ls.innings.length - 1 && ls.home_did_not_bat_last) v = 'X'
          }
          const on = liveOffense === side && i === currentIdx
          return (
            <td key={c} className={`${styles.lsCell} ${on ? styles.lsOn : ''}`}>
              {v}
            </td>
          )
        })}
        <td className={styles.lsGap} />
        <td className={`${styles.lsTot} ${styles.lsRuns}`}>{totals.runs ?? ''}</td>
        <td className={styles.lsTot}>{totals.hits ?? ''}</td>
        <td className={styles.lsTot}>{totals.errors ?? ''}</td>
      </tr>
    )
  }
  return (
    <Section title="Linescore">
      <div className={styles.board}>
        <table className={styles.lsTable}>
          <thead>
            <tr>
              <th scope="col" className={styles.lsTeam}>
                <span className="sr-only">Team</span>
              </th>
              {cols.map((c) => (
                <th scope="col" key={c} className={styles.lsHead}>
                  {c}
                </th>
              ))}
              <th className={styles.lsGap} />
              <th scope="col" className={`${styles.lsHead} ${styles.lsRHead}`}>
                R
              </th>
              <th scope="col" className={styles.lsHead}>
                H
              </th>
              <th scope="col" className={styles.lsHead}>
                E
              </th>
            </tr>
          </thead>
          <tbody>
            {row('away')}
            {row('home')}
          </tbody>
        </table>
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------------------
// On the field (live)
// ---------------------------------------------------------------------------

function Dots({ label, value, max, on }: { label: string; value: number; max: number; on: string }): React.ReactElement {
  return (
    <div className={styles.countGroup} aria-label={`${label} ${value}`}>
      <span className={styles.countLabel}>{label}</span>
      {Array.from({ length: max }, (_, i) => (
        <span
          key={i}
          className={styles.countDot}
          style={i < value ? { background: on, borderColor: on } : undefined}
        />
      ))}
    </div>
  )
}

function OnTheField({ game, feed }: { game: GameCard; feed: GameFeedCard }): React.ReactElement | null {
  const live = feed.live
  if (!live) return null
  const balls = Math.min(live.balls ?? 0, 3)
  const strikes = Math.min(live.strikes ?? 0, 2)
  const outs = Math.min(live.outs ?? 0, 2)
  return (
    <Section title="On the field">
      <CardField live={live} />
      <div className={styles.countRow}>
        <div className={styles.count}>
          <Dots label="B" value={balls} max={3} on="var(--sim-success-500)" />
          <Dots label="S" value={strikes} max={2} on="var(--dd-red)" />
          <Dots label="O" value={outs} max={2} on="var(--dd-red)" />
        </div>
        {live.pitcher && (
          <div className={styles.pitcher}>
            P <strong>{shortName(live.pitcher.name)}</strong>
            {live.pitcher.np != null && <span className={styles.mono}> · {live.pitcher.np} pitches</span>}
          </div>
        )}
      </div>
      {live.last_play && (
        <div className={styles.lastPlay}>
          <span className={styles.lastPlayLabel}>Last play</span>
          <span>{live.last_play}</span>
        </div>
      )}
      <span className="sr-only">{teamLabel(game, live.offense ?? 'away')} at bat</span>
    </Section>
  )
}

// ---------------------------------------------------------------------------
// Box score (live + final), one team per tab
// ---------------------------------------------------------------------------

const BAT_COLS = ['AB', 'R', 'H', 'RBI', 'BB', 'K', 'HR', 'AVG'] as const
const PIT_COLS = ['IP', 'H', 'R', 'ER', 'BB', 'K', 'NP', 'ERA'] as const

function BoxScore({ game, feed }: { game: GameCard; feed: GameFeedCard }): React.ReactElement | null {
  const offense = feed.live?.offense ?? null
  const [tab, setTab] = useState<'away' | 'home' | null>(null)
  if (!feed.box) return null
  const active = tab ?? offense ?? 'away'
  const side: FeedBoxSide = feed.box[active]
  const atBat = offense === active
  const role = feed.live ? (atBat ? 'At bat' : 'In the field') : ''
  const cur = feed.live?.batter?.id ?? null
  const v = (x: number | string | null | undefined): string => (x == null ? '' : String(x))
  return (
    <Section title="Box score">
      <div className={styles.tabs} role="tablist" aria-label="Box score team">
        {(['away', 'home'] as const).map((s) => (
          <button
            key={s}
            type="button"
            role="tab"
            aria-selected={active === s}
            className={`${styles.tab} ${active === s ? styles.tabOn : ''}`}
            onClick={() => setTab(s)}
          >
            {teamLabel(game, s)}
          </button>
        ))}
      </div>
      {role && <div className={`${styles.role} ${atBat ? styles.roleBat : ''}`}>{role}</div>}

      <div className={styles.boxGrid} role="table" aria-label="Batting">
        <div className={styles.boxHead} role="row">
          <div role="columnheader" className={styles.boxTitle}>
            Batting
          </div>
          {BAT_COLS.map((c) => (
            <div role="columnheader" key={c}>
              {c}
            </div>
          ))}
        </div>
        {side.batters.map((b) => (
          <div
            role="row"
            key={`${b.id}-${b.batting_order}`}
            className={`${styles.boxRow} ${atBat && b.id === cur ? styles.boxCur : ''}`}
          >
            <div role="cell" className={`${styles.boxName} ${b.is_sub ? styles.boxSub : ''}`}>
              {shortName(b.name)} <span className={styles.boxPos}>{b.pos}</span>
            </div>
            {[b.ab, b.r, b.h, b.rbi, b.bb, b.k, b.hr, b.avg].map((x, i) => (
              <div role="cell" key={i}>
                {v(x)}
              </div>
            ))}
          </div>
        ))}
      </div>

      <div className={styles.boxGrid} role="table" aria-label="Pitching">
        <div className={styles.boxHead} role="row">
          <div role="columnheader" className={styles.boxTitle}>
            Pitching
          </div>
          {PIT_COLS.map((c) => (
            <div role="columnheader" key={c}>
              {c}
            </div>
          ))}
        </div>
        {side.pitchers.map((p) => (
          <div role="row" key={p.id ?? p.name} className={styles.boxRow}>
            <div role="cell" className={styles.boxName}>
              {shortName(p.name)}
            </div>
            {[p.ip, p.h, p.r, p.er, p.bb, p.k, p.np, p.era].map((x, i) => (
              <div role="cell" key={i}>
                {v(x)}
              </div>
            ))}
          </div>
        ))}
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------------------
// Odds (preview + final)
// ---------------------------------------------------------------------------

function Odds({ game, odds, final }: { game: GameCard; odds: CardOdds; final: boolean }): React.ReactElement | null {
  const A = game.away_team_abbrev ?? 'Away'
  const H = game.home_team_abbrev ?? 'Home'
  const s = final ? odds.settled : null
  const books = new Set([odds.moneyline?.book, odds.runline?.book, odds.total?.book].filter(Boolean))
  const cells: Array<{ mkt: string; a: string; h: string; win: 'a' | 'h' | null }> = []
  if (odds.moneyline) {
    cells.push({
      mkt: 'Moneyline',
      a: `${A} ${american(odds.moneyline.away)}`,
      h: `${H} ${american(odds.moneyline.home)}`,
      win: s?.moneyline === 'away' ? 'a' : s?.moneyline === 'home' ? 'h' : null,
    })
  }
  if (odds.runline) {
    cells.push({
      mkt: 'Run line',
      a: `${signedLine(odds.runline.away.line)} ${american(odds.runline.away.price)}`,
      h: `${signedLine(odds.runline.home.line)} ${american(odds.runline.home.price)}`,
      win: s?.runline === 'away' ? 'a' : s?.runline === 'home' ? 'h' : null,
    })
  }
  if (odds.total) {
    cells.push({
      mkt: 'Total',
      a: `O ${odds.total.line} ${american(odds.total.over)}`,
      h: `U ${odds.total.line} ${american(odds.total.under)}`,
      win: s?.total === 'over' ? 'a' : s?.total === 'under' ? 'h' : null,
    })
  }
  if (cells.length === 0) return null

  const results: string[] = []
  if (s) {
    if (odds.moneyline && s.moneyline) {
      const team = s.moneyline === 'away' ? A : H
      results.push(`${team} ML ${american(s.moneyline === 'away' ? odds.moneyline.away : odds.moneyline.home)}`)
    }
    if (odds.runline && s.runline) {
      if (s.runline === 'push') results.push('Run line push')
      else {
        const team = s.runline === 'away' ? A : H
        results.push(`${team} ${signedLine(odds.runline[s.runline].line)} covered`)
      }
    }
    if (odds.total && s.total) {
      const runs = s.away_score + s.home_score
      results.push(
        s.total === 'push' ? `Total push · ${runs}` : `${s.total === 'over' ? 'Over' : 'Under'} ${odds.total.line} · ${runs} runs`,
      )
    }
  }

  return (
    <Section title={final ? 'Betting results · pregame lines' : 'Pregame odds'}>
      <div className={styles.odds}>
        {cells.map((c) => (
          <div key={c.mkt} className={styles.oddsCol}>
            <div className={styles.oddsMkt}>{c.mkt}</div>
            <div className={`${styles.oddsCell} ${c.win === 'a' ? styles.oddsWin : ''}`}>{c.a}</div>
            <div className={`${styles.oddsCell} ${c.win === 'h' ? styles.oddsWin : ''}`}>{c.h}</div>
          </div>
        ))}
      </div>
      {results.length > 0 && (
        <div className={styles.results}>
          {results.map((r) => (
            <span key={r} className={styles.result}>
              {r}
            </span>
          ))}
        </div>
      )}
      <div className={styles.oddsSource}>{[...books].join(' · ')} · {lineTypeLabel(odds)}</div>
    </Section>
  )
}

function lineTypeLabel(odds: CardOdds): string {
  const types = new Set([odds.moneyline?.line_type, odds.runline?.line_type, odds.total?.line_type].filter(Boolean))
  return types.has('closing') && types.size === 1 ? 'closing line' : types.has('closing') ? 'closing / latest' : 'latest line'
}
