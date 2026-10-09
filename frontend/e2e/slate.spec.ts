import { expect, test, type Page } from '@playwright/test'

/**
 * slate.spec.ts — SIM-519: the Daily Diamond slate.
 *
 * One date with a card in each state (live, scheduled, final, postponed), the
 * league feed and the card odds mocked per game. Checks the state tags, the
 * sort, the live card's default open state and its field, the box-score tabs,
 * the settled lines, the lineups, the calendar, the degraded banner and the
 * phone layout.
 */

const DATE = '2024-08-15'

const team = (side: 'home' | 'away', id: number, abbrev: string, name: string, w: number, l: number) => ({
  [`${side}_team_id`]: id,
  [`${side}_team_abbrev`]: abbrev,
  [`${side}_team_name`]: name,
  [`${side}_wins`]: w,
  [`${side}_losses`]: l,
})

const base = (pk: number, extra: Record<string, unknown>) => ({
  game_pk: pk,
  season: 2024,
  game_date: DATE,
  status: null,
  venue_id: null,
  venue_name: null,
  venue_city: null,
  ...extra,
})

const GAMES = [
  base(1, {
    ...team('away', 111, 'BOS', 'Red Sox', 65, 55),
    ...team('home', 147, 'NYY', 'Yankees', 70, 50),
    game_status: 'final',
    away_score: 3,
    home_score: 5,
    n_innings: 10,
    start_utc: '2024-08-15T17:05:00Z',
  }),
  base(2, {
    ...team('away', 137, 'SF', 'Giants', 60, 60),
    ...team('home', 119, 'LAD', 'Dodgers', 80, 40),
    game_status: 'scheduled',
    start_utc: '2024-08-16T02:10:00Z',
    double_header: 'S',
    game_number: 2,
    away_probable_pitcher_name: 'Logan Webb',
    home_probable_pitcher_name: 'Tyler Glasnow',
    sim_summary: {
      n_iterations: 100,
      home_win_pct: 0.61,
      away_win_pct: 0.39,
      home_score_mean: 4.9,
      away_score_mean: 3.8,
      simulated_at: '2024-08-15T12:00:00Z',
    },
    sim_run_at: '2024-08-15T12:00:00Z',
  }),
  base(3, {
    ...team('away', 116, 'DET', 'Tigers', 59, 62),
    ...team('home', 136, 'SEA', 'Mariners', 62, 59),
    game_status: 'live',
    away_score: 1,
    home_score: 2,
    inning: 6,
    inning_half: 'Top',
    outs: 1,
    start_utc: '2024-08-15T23:40:00Z',
  }),
  base(4, {
    ...team('away', 121, 'NYM', 'Mets', 62, 59),
    ...team('home', 120, 'WSH', 'Nationals', 54, 67),
    game_status: 'postponed',
    detailed_state: 'Postponed',
    reason: 'Rain',
    rescheduled_to: '2024-08-17',
  }),
]

const person = (id: number, name: string) => ({ id, name })
const batter = (id: number, name: string, pos: string, order: number) => ({
  id,
  name,
  pos,
  batting_order: order * 100,
  is_sub: false,
  ab: 3,
  r: 0,
  h: 1,
  rbi: 0,
  bb: 0,
  k: 1,
  hr: 0,
  avg: '.250',
})
const pitcher = (id: number, name: string) => ({
  id,
  name,
  outs: 15,
  ip: '5.0',
  h: 4,
  r: 1,
  er: 1,
  bb: 2,
  k: 6,
  np: 88,
  era: '3.10',
})
const box = (prefix: number) => ({
  batters: Array.from({ length: 9 }, (_, i) => batter(prefix + i, `Player${prefix + i} Last${prefix + i}`, 'CF', i + 1)),
  pitchers: [pitcher(prefix + 50, `Arm${prefix} Starter${prefix}`)],
})

const FEEDS: Record<number, unknown> = {
  1: {
    game_pk: 1,
    status: 'final',
    detailed_state: 'Final',
    linescore: {
      innings: Array.from({ length: 10 }, (_, i) => ({ num: i + 1, away: i === 2 ? 3 : 0, home: i === 9 ? 2 : i === 4 ? 3 : 0 })),
      away: { runs: 3, hits: 7, errors: 0 },
      home: { runs: 5, hits: 9, errors: 1 },
      home_did_not_bat_last: false,
      current_inning: null,
      inning_half: null,
    },
    lineups: { away: [], home: [], away_probable_pitcher: null, home_probable_pitcher: null },
    box: { away: box(100), home: box(200) },
    live: null,
    source: 'feed',
    feed_error: null,
  },
  2: {
    game_pk: 2,
    status: 'scheduled',
    detailed_state: 'Scheduled',
    linescore: null,
    lineups: {
      away: [],
      home: Array.from({ length: 9 }, (_, i) => ({ order: i + 1, id: 300 + i, name: `Dodger${i} Hitter${i}`, pos: 'SS' })),
      away_probable_pitcher: person(1, 'Logan Webb'),
      home_probable_pitcher: person(2, 'Tyler Glasnow'),
    },
    box: null,
    live: null,
    source: 'feed',
    feed_error: null,
  },
  3: {
    game_pk: 3,
    status: 'live',
    detailed_state: 'In Progress',
    linescore: {
      innings: Array.from({ length: 6 }, (_, i) => ({ num: i + 1, away: i === 0 ? 1 : 0, home: i === 5 ? null : i === 3 ? 2 : 0 })),
      away: { runs: 1, hits: 4, errors: 0 },
      home: { runs: 2, hits: 5, errors: 0 },
      home_did_not_bat_last: false,
      current_inning: 6,
      inning_half: 'Top',
    },
    lineups: { away: [], home: [], away_probable_pitcher: null, home_probable_pitcher: null },
    box: { away: box(400), home: box(500) },
    live: {
      balls: 2,
      strikes: 1,
      outs: 1,
      offense: 'away',
      runners: { first: person(401, 'Riley Greene'), second: null, third: null },
      batter: person(402, 'Player402 Last402'),
      pitcher: { id: 550, name: 'Logan Gilbert', np: 77 },
      fielders: { P: 'Logan Gilbert', C: 'Cal Raleigh', '1B': 'Justin Turner', '2B': 'Jorge Polanco', '3B': 'Josh Rojas', SS: 'J.P. Crawford', LF: 'Randy Arozarena', CF: 'Julio Rodríguez', RF: 'Victor Robles' },
      last_play: 'Riley Greene singles on a line drive to right fielder Victor Robles.',
    },
    source: 'feed',
    feed_error: null,
  },
}

const ODDS: Record<number, unknown> = {
  1: {
    game_pk: 1,
    moneyline: { book: 'DraftKings', line_type: 'closing', away: 130, home: -150 },
    runline: { book: 'DraftKings', line_type: 'closing', away: { line: 1.5, price: -160 }, home: { line: -1.5, price: 135 } },
    total: { book: 'DraftKings', line_type: 'closing', line: 8.5, over: -110, under: -110 },
    settled: { away_score: 3, home_score: 5, moneyline: 'home', runline: 'home', total: 'under' },
  },
  2: {
    game_pk: 2,
    moneyline: { book: 'FanDuel', line_type: 'current', away: 145, home: -170 },
    runline: null,
    total: { book: 'FanDuel', line_type: 'current', line: 8, over: -105, under: -115 },
    settled: null,
  },
}

async function mockAll(page: Page, opts: { source?: string; feedError?: string } = {}): Promise<void> {
  await page.route('**/auth/me', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ authenticated: true, mode: 'session', expires_in: 3600 }) }),
  )
  await page.route(`**/api/games/${DATE}`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ date: DATE, count: GAMES.length, games: GAMES, source: opts.source ?? 'schedule', feed_error: opts.feedError ?? null }),
    }),
  )
  await page.route('**/api/games/*/feed', (route) => {
    const pk = Number(route.request().url().split('/').at(-2))
    const body = FEEDS[pk]
    return body
      ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      : route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"down"}' })
  })
  await page.route('**/api/betting/games/*/card-odds', (route) => {
    const pk = Number(route.request().url().split('/').at(-2))
    const body = ODDS[pk]
    return body
      ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      : route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"none"}' })
  })
}

test('the cards sort live, scheduled, final, postponed, each with its tag', async ({ page }) => {
  await mockAll(page)
  await page.goto(`/date/${DATE}`)
  const cards = page.locator('[data-testid^="game-card-"]')
  await expect(cards).toHaveCount(4)
  await expect(cards.nth(0)).toHaveAttribute('data-testid', 'game-card-3')
  await expect(cards.nth(1)).toHaveAttribute('data-testid', 'game-card-2')
  await expect(cards.nth(2)).toHaveAttribute('data-testid', 'game-card-1')
  await expect(cards.nth(3)).toHaveAttribute('data-testid', 'game-card-4')

  await expect(page.getByTestId('game-card-3').getByText('Top 6th · 1 out')).toBeVisible()
  await expect(page.getByTestId('game-card-1').getByText('Final/10')).toBeVisible()
  await expect(page.getByTestId('game-card-2').getByText(/Game 2/)).toBeVisible()
  await expect(page.getByTestId('game-card-4').getByText('PPD · Rain')).toBeVisible()
  await expect(page.getByTestId('game-card-1').getByText('3 – 5')).toBeVisible()
  await expect(page.getByTestId('game-card-2').getByText('vs', { exact: true })).toBeVisible()
  // Our sim line, and its absence.
  await expect(page.getByTestId('game-card-2').getByTestId('sim-line')).toContainText('LAD 61%')
  await expect(page.getByTestId('game-card-1').getByTestId('sim-line')).toHaveText('Not simulated')
  // The summary tags.
  await expect(page.getByText('4 games')).toBeVisible()
  await expect(page.getByText('1 live')).toBeVisible()
  await expect(page.getByText('1 final')).toBeVisible()
})

test('a live card opens by default and shows the field, count and last play', async ({ page }) => {
  await mockAll(page)
  await page.goto(`/date/${DATE}`)
  const live = page.getByTestId('game-card-3')
  await expect(live.getByRole('button', { name: /Tigers at Mariners/ })).toHaveAttribute('aria-expanded', 'true')
  await expect(live.getByRole('img', { name: /Runners on first/ })).toBeVisible()
  await expect(live.getByText('Raleigh')).toBeVisible()
  await expect(live.getByText(/1B\s*Greene/)).toBeVisible()
  await expect(live.getByText(/77 pitches/)).toBeVisible()
  await expect(live.getByText(/Riley Greene singles/)).toBeVisible()
  await expect(live.getByLabel('B 2')).toBeVisible()
  // The box score opens on the batting side and marks it.
  await expect(live.getByRole('tab', { name: 'DET Tigers' })).toHaveAttribute('aria-selected', 'true')
  await expect(live.getByText('At bat', { exact: true })).toBeVisible()
  // Other cards stay closed.
  await expect(page.getByTestId('game-card-1').getByRole('button').first()).toHaveAttribute('aria-expanded', 'false')
})

test('a final card shows the linescore, switches box tabs, and settles the lines', async ({ page }) => {
  await mockAll(page)
  await page.goto(`/date/${DATE}`)
  const card = page.getByTestId('game-card-1')
  await card.getByRole('button', { name: /Red Sox at Yankees/ }).click()
  await expect(card.getByRole('region', { name: 'Linescore' })).toBeVisible()
  await expect(card.getByRole('columnheader', { name: '10' })).toBeVisible()
  await expect(card.getByText('P. Last100')).toBeVisible()
  await card.getByRole('tab', { name: 'NYY Yankees' }).click()
  await expect(card.getByText('P. Last200')).toBeVisible()
  await expect(card.getByText('Betting results · pregame lines')).toBeVisible()
  await expect(card.getByText('NYY ML -150')).toBeVisible()
  await expect(card.getByText('NYY -1.5 covered')).toBeVisible()
  await expect(card.getByText('Under 8.5 · 8 runs')).toBeVisible()
})

test('a scheduled card shows the lineups, the probables and the pregame odds', async ({ page }) => {
  await mockAll(page)
  await page.goto(`/date/${DATE}`)
  const card = page.getByTestId('game-card-2')
  await card.getByRole('button', { name: /Giants at Dodgers/ }).click()
  await expect(card.getByText('Lineup not yet posted')).toBeVisible()
  await expect(card.getByText('L. Webb')).toBeVisible()
  await expect(card.getByText('D. Hitter0')).toBeVisible()
  await expect(card.getByText('Pregame odds')).toBeVisible()
  await expect(card.getByText('LAD -170')).toBeVisible()
  await expect(card.getByText('O 8 -105')).toBeVisible()
})

test('a postponed card names the make-up date; the open card links to the game page', async ({ page }) => {
  await mockAll(page)
  await page.goto(`/date/${DATE}`)
  const card = page.getByTestId('game-card-4')
  await card.getByRole('button', { name: /Mets at Nationals/ }).click()
  await expect(card.getByText(/Rescheduled to Saturday, August 17/)).toBeVisible()
  await expect(card.getByRole('link', { name: /Open game/ })).toHaveAttribute('href', '/game/4')
})

test('the calendar picks a date and Today comes back', async ({ page }) => {
  await mockAll(page)
  await page.route('**/api/games/2024-08-20', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ date: '2024-08-20', count: 0, games: [], source: 'schedule' }) }),
  )
  await page.goto(`/date/${DATE}`)
  await page.getByRole('button', { name: /Choose a date/ }).click()
  await page.getByRole('dialog', { name: 'Calendar' }).getByRole('button', { name: /August 20/ }).click()
  await expect(page).toHaveURL(/\/date\/2024-08-20/)
  await expect(page.getByText(/No MLB games on/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Today' })).toBeVisible()
})

test('a degraded read shows the banner', async ({ page }) => {
  await mockAll(page, { source: 'db', feedError: 'TimeoutError: read timed out' })
  await page.goto(`/date/${DATE}`)
  await expect(page.getByRole('status').filter({ hasText: 'Showing stored games' })).toBeVisible()
  await expect(page.getByText(/TimeoutError/)).toBeVisible()
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 375, height: 812 } })

  test('every card starts closed and the page does not scroll sideways', async ({ page }) => {
    await mockAll(page)
    await page.goto(`/date/${DATE}`)
    await expect(page.locator('[data-testid^="game-card-"]')).toHaveCount(4)
    await expect(page.getByTestId('game-card-3').getByRole('button').first()).toHaveAttribute('aria-expanded', 'false')
    const scroll = await page.evaluate(() => document.documentElement.scrollWidth)
    expect(scroll).toBeLessThanOrEqual(375)
  })
})
