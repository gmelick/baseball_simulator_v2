import { expect, test, type Page } from '@playwright/test'

/**
 * gamepage.spec.ts — the game page review (2026-10-09).
 *
 * A final game opens with the real game (the league feed's linescore, box score
 * and play-by-play) above the simulation. "What if from here" on a play opens the
 * what-if panel at that plate appearance; a staged reliever runs the two 100-game
 * runs and the panel compares them. Projections and Betting start collapsed and
 * the browser remembers an opened panel. The Betting card grades each side on
 * the real final and marks a market with no stored line. The Line movement and
 * Managerial override boxes are gone. The backend is mocked with page.route.
 */

const PK = 746437

const person = (id: number, name: string) => ({ id, name })

function live(over: Record<string, unknown> = {}) {
  return {
    balls: 0,
    strikes: 0,
    outs: 1,
    offense: 'away',
    runners: { first: person(11, 'Jarren Duran'), second: null, third: null },
    batter: person(12, 'Rafael Devers'),
    pitcher: { ...person(21, 'Gerrit Cole'), np: 88 },
    fielders: { C: 'Wells', '1B': 'Rizzo', '2B': 'Torres', '3B': 'Chisholm', SS: 'Volpe', LF: 'Verdugo', CF: 'Bellinger', RF: 'Judge' },
    last_play: null,
    ...over,
  }
}

const opt = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  position: null,
  bats: null,
  throws: null,
  ...extra,
})

function side(prefix: number, pitcher: ReturnType<typeof opt>, bullpen: ReturnType<typeof opt>[]) {
  return {
    lineup: Array.from({ length: 9 }, (_, i) => ({ slot: i, id: prefix + i, name: `Player ${prefix + i}`, position: 'DH' })),
    pitcher,
    defense: {},
    bench: [opt(prefix + 50, `Bench ${prefix + 50}`, { bats: 'L' })],
    bullpen,
    used: [],
  }
}

function play(at_bat: number, over: Record<string, unknown> = {}) {
  return {
    at_bat,
    inning: 7,
    half: 'top',
    batter_id: 12,
    pitcher_id: 21,
    event: 'Single',
    event_type: 'single',
    description: 'Rafael Devers singles on a line drive to right fielder Aaron Judge.',
    rbi: 0,
    batter_finished: true,
    outs_before: 1,
    outs_after: 1,
    away_score_before: 3,
    home_score_before: 2,
    away_score_after: 3,
    home_score_after: 2,
    runners: { first: 11, second: null, third: null },
    pitches: [{ call: 'Ball', type: 'Four-Seam Fastball', speed: 97.1 }],
    is_complete: true,
    ...over,
  }
}

function run(run_id: number, status: string, summary: Record<string, number> | null) {
  return {
    run_id,
    game_pk: PK,
    status,
    n_iterations: 100,
    progress_done: status === 'done' ? 100 : 30,
    base_seed: 77,
    position_in_queue: null,
    existing: false,
    requested_at: null,
    started_at: null,
    finished_at: null,
    error: null,
    lineup_source: null,
    bullpen_source: null,
    replay_run_id: null,
    summary: summary ? { n_iterations: 100, simulated_at: '2026-10-09T00:00:00Z', ...summary } : null,
  }
}

async function json(page: Page, glob: string, body: unknown, status = 200): Promise<void> {
  await page.route(glob, (route) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }),
  )
}

async function mockFinalGame(page: Page): Promise<{ whatIfBody: () => unknown }> {
  await json(page, '**/auth/me', { authenticated: true, mode: 'session', expires_in: 3600 })
  await json(page, `**/api/games/${PK}/status`, {
    game_pk: PK,
    game_status: 'final',
    game_date: '2026-09-20',
    season: 2026,
    home_team_name: 'Yankees',
    home_team_abbrev: 'NYY',
    away_team_name: 'Red Sox',
    away_team_abbrev: 'BOS',
    home_score_final: 2,
    away_score_final: 4,
    sim_summary: null,
    odds: null,
  })
  for (const sub of ['linescore', 'plays', 'live', 'card']) {
    await json(page, `**/api/games/${PK}/${sub}**`, { detail: 'none' }, 404)
  }
  await json(page, `**/api/games/${PK}/simulate/runs/latest`, { detail: 'none' }, 404)
  await json(page, `**/api/games/${PK}/feed`, {
    game_pk: PK,
    status: 'final',
    detailed_state: 'Final',
    linescore: {
      innings: Array.from({ length: 9 }, (_, i) => ({ num: i + 1, away: i === 6 ? 3 : i === 0 ? 1 : 0, home: i === 3 ? 2 : 0 })),
      away: { runs: 4, hits: 9, errors: 0 },
      home: { runs: 2, hits: 6, errors: 1 },
      home_did_not_bat_last: false,
      current_inning: 9,
      inning_half: 'Bottom',
    },
    lineups: { away: [], home: [], away_probable_pitcher: null, home_probable_pitcher: null },
    box: {
      away: {
        batters: [{ id: 12, name: 'Rafael Devers', pos: '3B', batting_order: 300, is_sub: false, ab: 4, r: 1, h: 2, rbi: 1, bb: 0, k: 1, hr: 0, avg: '.281' }],
        pitchers: [{ id: 31, name: 'Brayan Bello', outs: 18, ip: '6.0', h: 5, r: 2, er: 2, bb: 1, k: 6, np: 95, era: '3.90' }],
      },
      home: {
        batters: [{ id: 99, name: 'Aaron Judge', pos: 'RF', batting_order: 200, is_sub: false, ab: 4, r: 1, h: 1, rbi: 2, bb: 0, k: 2, hr: 1, avg: '.310' }],
        pitchers: [{ id: 21, name: 'Gerrit Cole', outs: 20, ip: '6.2', h: 7, r: 4, er: 4, bb: 2, k: 7, np: 104, era: '3.41' }],
      },
    },
    live: null,
    source: 'feed',
  })
  await json(page, `**/api/games/${PK}/feed/plays`, {
    game_pk: PK,
    status: 'final',
    plays: [
      play(0, { inning: 1, batter_id: 13, description: 'Jarren Duran grounds out.', event: 'Groundout', outs_after: 1, outs_before: 0, runners: { first: null, second: null, third: null } }),
      play(41),
      play(42, { event: 'Home Run', description: 'Trevor Story homers.', batter_id: 14, away_score_after: 5, rbi: 2 }),
    ],
    names: { '11': 'Jarren Duran', '12': 'Rafael Devers', '13': 'Jarren Duran', '14': 'Trevor Story', '21': 'Gerrit Cole' },
    source: 'feed',
  })
  await json(page, `**/api/games/${PK}/feed/state**`, {
    game_pk: PK,
    at_bat: 41,
    inning: 7,
    half: 'top',
    outs: 1,
    away_score: 3,
    home_score: 2,
    batting_side: 'away',
    live: live(),
    away: side(100, opt(31, 'Brayan Bello', { throws: 'R' }), []),
    home: side(200, opt(21, 'Gerrit Cole', { throws: 'R' }), [opt(25, 'Clay Holmes', { throws: 'R' }), opt(26, 'Tim Hill', { throws: 'L' })]),
  })
  let whatIfBody: unknown = null
  await page.route(`**/api/games/${PK}/what-if`, (route) => {
    whatIfBody = route.request().postDataJSON()
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ game_pk: PK, at_bat: 41, base_run_id: 501, change_run_id: 502, base_seed: 77, changes: ['NYY: Clay Holmes pitches for Gerrit Cole'] }),
    })
  })
  let polls = 0
  await page.route(`**/api/games/${PK}/what-if/501/502`, (route) => {
    polls += 1
    const done = polls > 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        game_pk: PK,
        base: done ? run(501, 'done', { home_win_pct: 0.3, away_win_pct: 0.7, home_score_mean: 3.1, away_score_mean: 4.4 }) : run(501, 'running', null),
        change: done ? run(502, 'done', { home_win_pct: 0.36, away_win_pct: 0.64, home_score_mean: 3.1, away_score_mean: 4.0 }) : run(502, 'running', null),
        real_final: { away: 4, home: 2 },
        start_score: { away: 3, home: 2 },
      }),
    })
  })
  // Betting: the moneyline from the stored lines (graded), the total with no stored line.
  const edge = (label: string, sideName: string, line: number | null, result: string | null, offered: number) => ({
    label,
    side: sideName,
    line,
    sim_prob: 0.55,
    market_fair_prob: 0.5,
    edge: 0.05,
    ev: 0.04,
    offered_american: offered,
    sim_fair_american: -122,
    clv: null,
    positive_edge: true,
    price_book: null,
    price_book_name: null,
    result,
  })
  const edges = {
    game_pk: PK,
    n_iterations: 200,
    base_seed: null,
    edges: [
      edge('moneyline', 'home', null, 'lost', -130),
      edge('moneyline', 'away', null, 'won', 110),
      edge('total', 'over', 8.5, 'lost', 999),
      edge('total', 'under', 8.5, 'won', 999),
    ],
    odds_source: { moneyline: 'stored', total: 'mock' },
    run_line_pricing: null,
    fair_book: {},
    fair_book_name: {},
    market_names: { moneyline: 'Moneyline', total: 'Total' },
    markets: ['moneyline', 'total'],
    final: { away: 4, home: 2 },
  }
  await json(page, `**/api/betting/games/${PK}/edges**`, edges)
  await json(page, `**/api/betting/games/${PK}/signals**`, { ...edges, config: {}, signals: [] })
  return { whatIfBody: () => whatIfBody }
}

test('a final game shows the real game first and no old boxes', async ({ page }) => {
  await mockFinalGame(page)
  await page.goto(`/game/${PK}`)

  const real = page.getByTestId('real-game')
  await expect(real.getByRole('heading', { name: 'The game' })).toBeVisible()
  await expect(real.getByText('Rafael Devers').first()).toBeVisible()
  await expect(real.getByText('B. Bello').first()).toBeVisible()
  await expect(real.getByTestId('real-play-42')).toContainText('Trevor Story homers.')

  // The real game sits above every simulation section.
  const realBox = await real.boundingBox()
  const simBox = await page.getByRole('region', { name: 'Simulation' }).boundingBox()
  expect(realBox && simBox && realBox.y < simBox.y).toBeTruthy()

  await expect(page.getByText('Line movement')).toHaveCount(0)
  await expect(page.getByText('Managerial override')).toHaveCount(0)
})

test('Projections and Betting start collapsed, and an opened panel is remembered', async ({ page }) => {
  await mockFinalGame(page)
  await page.goto(`/game/${PK}`)

  const toggle = page.getByRole('button', { name: 'Betting', exact: true })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('button', { name: 'Projections', exact: true })).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('button', { name: 'Load betting' })).toBeHidden()

  await toggle.click()
  await expect(page.getByRole('button', { name: 'Load betting' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: 'Betting', exact: true })).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('button', { name: 'Projections', exact: true })).toHaveAttribute('aria-expanded', 'false')
})

test('What if from a play: the field, a reliever, and the two runs compared', async ({ page }) => {
  const mocks = await mockFinalGame(page)
  await page.goto(`/game/${PK}`)

  await page.getByTestId('real-play-41').getByRole('button', { name: /What if/ }).click()
  const panel = page.getByTestId('what-if')
  await expect(panel.getByText('Top 7th, 1 out, BOS 3–2 NYY')).toBeVisible()
  await expect(panel.getByText(/BOS batting · P Gerrit Cole \(88 pitches\)/)).toBeVisible()

  await panel.getByLabel('New pitcher: player').selectOption({ label: 'Clay Holmes (RHP)' })
  await panel.getByRole('group', { name: 'New pitcher' }).getByRole('button', { name: 'Stage' }).click()
  await expect(panel.getByText('NYY: Clay Holmes pitches for Gerrit Cole')).toBeVisible()

  await panel.getByRole('button', { name: 'Simulate 100 from here' }).click()
  await expect(panel.getByRole('columnheader', { name: 'With changes' })).toBeVisible({ timeout: 10_000 })
  await expect(panel.getByRole('row', { name: /NYY win/ })).toContainText('+6.0 pts')
  await expect(panel.getByText(/Real final: BOS 4–2 NYY/)).toBeVisible()
  expect(mocks.whatIfBody()).toMatchObject({ at_bat: 41, changes: { pitcher: { player_id: 25 } }, n_iterations: 100 })
})

test('the Betting card grades each side on the real final and marks a market with no line', async ({ page }) => {
  await mockFinalGame(page)
  await page.goto(`/game/${PK}`)

  const card = page.getByRole('region', { name: 'Betting' })
  await card.getByRole('button', { name: 'Betting', exact: true }).click()
  await card.getByRole('button', { name: 'Load betting' }).click()

  await expect(card.getByTestId('betting-final')).toContainText('Final 4–2')
  const ml = card.getByRole('region', { name: 'Moneyline' })
  await expect(ml.locator('[data-result="won"]')).toContainText('Away')
  await expect(ml.locator('[data-result="lost"]')).toContainText('Home')
  const total = card.getByRole('region', { name: 'Total' })
  await expect(total.getByText('no stored line')).toBeVisible()
  // No invented price, and no grade against an invented line.
  await expect(total.getByText('+999')).toHaveCount(0)
  await expect(total.locator('[data-result]')).toHaveCount(0)
})
