import { expect, test, type Page } from '@playwright/test'

/**
 * smoke.spec.ts — SIM-400
 *
 * Cross-browser smoke tests for the frontend, run against the built preview
 * server with the backend fully mocked via page.route. These verify rendering,
 * routing, and auth-gating in isolation — no live API/DB required.
 */

const DATE = '2024-08-15'

/** Mock GET /auth/me as an active session. */
async function mockAuthed(page: Page): Promise<void> {
  await page.route('**/auth/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ authenticated: true, mode: 'session', expires_in: 3600 }),
    }),
  )
}

/** Mock the slate for DATE with two games (one final, one scheduled). */
async function mockSlate(page: Page): Promise<void> {
  await page.route(`**/api/games/${DATE}`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        date: DATE,
        count: 2,
        games: [
          {
            game_pk: 745001,
            season: 2024,
            game_date: DATE,
            status: 'Final',
            home_team_id: 147,
            away_team_id: 111,
            venue_id: 3313,
            home_team_name: 'Yankees',
            home_team_abbrev: 'NYY',
            away_team_name: 'Red Sox',
            away_team_abbrev: 'BOS',
            venue_name: 'Yankee Stadium',
            venue_city: 'Bronx',
            home_wins: 70,
            home_losses: 50,
            away_wins: 65,
            away_losses: 55,
          },
          {
            game_pk: 745002,
            season: 2024,
            game_date: DATE,
            status: 'Preview',
            home_team_id: 119,
            away_team_id: 137,
            venue_id: 22,
            home_team_name: 'Dodgers',
            home_team_abbrev: 'LAD',
            away_team_name: 'Giants',
            away_team_abbrev: 'SF',
            venue_name: 'Dodger Stadium',
            venue_city: 'Los Angeles',
            home_wins: 80,
            home_losses: 40,
            away_wins: 60,
            away_losses: 60,
          },
        ],
      }),
    }),
  )
}

/** Mock the per-game aggregate + the (empty) sub-resources the Game page reads. */
async function mockGame(page: Page): Promise<void> {
  await page.route('**/api/games/745001/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        game_pk: 745001,
        game_status: 'final',
        game_date: DATE,
        season: 2024,
        home_team_name: 'Yankees',
        home_team_abbrev: 'NYY',
        away_team_name: 'Red Sox',
        away_team_abbrev: 'BOS',
        home_score_final: 5,
        away_score_final: 3,
        sim_summary: null,
        odds: null,
      }),
    }),
  )
  // Optional sub-resources: a 404 is the "no data yet" path the page tolerates.
  for (const sub of ['linescore', 'plays', 'live']) {
    await page.route(`**/api/games/745001/${sub}`, (route) =>
      route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"none"}' }),
    )
  }
  await page.route('**/api/betting/games/745001/line-movement**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ game_pk: 745001, market_type: 'moneyline', book: null, count: 0, series: [] }),
    }),
  )
}

test('unauthenticated visitor sees the login page', async ({ page }) => {
  await page.route('**/auth/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ authenticated: false, mode: 'unauthenticated', expires_in: 0 }),
    }),
  )
  await page.goto('/')
  await expect(page.getByLabel('Log in to MLB Sim Platform')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
})

test('day summary renders the slate with a game count', async ({ page }) => {
  await mockAuthed(page)
  await mockSlate(page)
  await page.goto(`/date/${DATE}`)

  // SIM-519: the Daily Diamond slate. An old payload (no game_status) still
  // maps its stored status to a card state.
  await expect(page.getByRole('heading', { name: 'Thursday, August 15' })).toBeVisible()
  await expect(page.getByText('2 games')).toBeVisible()
  await expect(page.getByText('Yankees')).toBeVisible()
  await expect(page.getByText('Dodgers')).toBeVisible()
  await expect(page.getByTestId('game-card-745001').getByText('Final', { exact: true }).first()).toBeVisible()
  await expect(page.getByTestId('game-card-745002').getByText('Pregame')).toBeVisible()
})

test('an open card links to the game page', async ({ page }) => {
  await mockAuthed(page)
  await mockSlate(page)
  await mockGame(page)
  // SIM-519: the open card reads the feed and the card odds; none here.
  await page.route('**/api/games/745001/feed', (route) =>
    route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"down"}' }),
  )
  await page.route('**/api/betting/games/745001/card-odds', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"none"}' }),
  )
  await page.goto(`/date/${DATE}`)

  // A click opens the card in place; its "Open game" link goes to the game page.
  await page.getByRole('button', { name: /Red Sox at Yankees/i }).click()
  await page.getByRole('link', { name: /Open game/i }).click()
  await expect(page).toHaveURL(/\/game\/745001/)
  await expect(page.getByText('FINAL')).toBeVisible()
  await expect(page.getByRole('link', { name: /Back to slate/i })).toBeVisible()
})

test('date navigation advances the day', async ({ page }) => {
  await mockAuthed(page)
  await mockSlate(page)
  // Next-day slate (empty) so we can assert the URL + empty state.
  await page.route('**/api/games/2024-08-16', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ date: '2024-08-16', count: 0, games: [] }),
    }),
  )
  await page.goto(`/date/${DATE}`)
  await page.getByRole('button', { name: 'Next day' }).click()
  await expect(page).toHaveURL(/\/date\/2024-08-16/)
  await expect(page.getByText(/No MLB games on/i)).toBeVisible()
})

/**
 * SIM-546: the fifteen game markets, as the edge route answers with the mock
 * provider (every market priced from the mock's prices). Each market carries its
 * kind's sides: a three-way market adds the tie ('draw').
 */
const EDGE_MARKETS: Array<{ type: string; label: string; name: string; sides: string[] }> = [
  { type: 'moneyline', label: 'moneyline', name: 'Moneyline', sides: ['home', 'away'] },
  { type: 'runline', label: 'run_line', name: 'Run line', sides: ['home', 'away'] },
  { type: 'total', label: 'total', name: 'Total', sides: ['over', 'under'] },
  { type: 'f1_moneyline', label: 'f1_moneyline', name: 'First inning moneyline', sides: ['home', 'away', 'draw'] },
  { type: 'f5_moneyline', label: 'f5_moneyline', name: 'First five moneyline', sides: ['home', 'away', 'draw'] },
  { type: 'f1_total', label: 'f1_total', name: 'First inning total', sides: ['over', 'under'] },
  { type: 'f5_total', label: 'f5_total', name: 'First five total', sides: ['over', 'under'] },
  { type: 'f1_runline', label: 'f1_runline', name: 'First inning run line', sides: ['home', 'away'] },
  { type: 'f5_runline', label: 'f5_runline', name: 'First five run line', sides: ['home', 'away'] },
  { type: 'team_total_home', label: 'team_total_home', name: 'Home team total', sides: ['over', 'under'] },
  { type: 'team_total_away', label: 'team_total_away', name: 'Away team total', sides: ['over', 'under'] },
  { type: 'f5_team_total_home', label: 'f5_team_total_home', name: 'Home team first five total', sides: ['over', 'under'] },
  { type: 'f5_team_total_away', label: 'f5_team_total_away', name: 'Away team first five total', sides: ['over', 'under'] },
  { type: 'first_to_score', label: 'first_to_score', name: 'First team to score', sides: ['home', 'away'] },
  { type: 'first_inning_run', label: 'first_inning_run', name: 'A run in the first inning', sides: ['over', 'under'] },
]

/** Mock /edges + /signals for 745001: every market from the mock provider, no signal. */
async function mockBetting(page: Page): Promise<void> {
  const lineOf = (label: string, side: string): number | null => {
    if (side === 'over' || side === 'under') return label === 'first_inning_run' ? 0.5 : 4.5
    if (label.endsWith('run_line') || label.endsWith('runline')) return side === 'home' ? -0.5 : 0.5
    return null
  }
  const edges = EDGE_MARKETS.flatMap((m) =>
    m.sides.map((side) => ({
      label: m.label,
      side,
      line: lineOf(m.label, side),
      sim_prob: 0.4,
      market_fair_prob: 0.42,
      edge: -0.02,
      ev: -0.05,
      offered_american: 120,
      sim_fair_american: 150,
      clv: null,
      positive_edge: false,
      price_book: null,
      price_book_name: null,
    })),
  )
  const oddsSource = Object.fromEntries(EDGE_MARKETS.map((m) => [m.type, 'mock']))
  const marketNames = Object.fromEntries(EDGE_MARKETS.map((m) => [m.label, m.name]))
  const pairPricing = { shape: 'pair', home_line: -0.5, away_line: 0.5, reference_margin: null, reference_source: null }
  const common = {
    game_pk: 745001,
    n_iterations: 200,
    base_seed: null,
    odds_source: oddsSource,
    run_line_pricing: { ...pairPricing, home_line: -1.5, away_line: 1.5 },
    fair_book: {},
    fair_book_name: {},
    market_names: marketNames,
    run_line_pricing_by_label: {
      run_line: { ...pairPricing, home_line: -1.5, away_line: 1.5 },
      f1_runline: pairPricing,
      f5_runline: pairPricing,
    },
  }
  await page.route('**/api/betting/games/745001/edges**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...common, markets: EDGE_MARKETS.map((m) => m.type), edges }),
    }),
  )
  await page.route('**/api/betting/games/745001/signals**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...common, config: {}, signals: [] }),
    }),
  )
}

test('the betting card renders all fifteen game markets with a tie side', async ({ page }) => {
  await mockAuthed(page)
  await mockGame(page)
  await mockBetting(page)
  await page.goto('/game/745001')

  const card = page.getByRole('region', { name: 'Betting' })
  await card.getByRole('button', { name: 'Load betting' }).click()

  // SIM-546: one section per market, titled from market_names.
  await expect(card.locator('section')).toHaveCount(15)
  // The four sub-headings, in the card's order.
  await expect(card.locator('h4')).toHaveText([
    'Full game',
    'First five innings',
    'First inning',
    'Team totals',
  ])
  // The sections, grouped under their sub-headings. The mock lists the markets in
  // the vocabulary order, so this order holds only when the card groups them.
  await expect(card.locator('section h5')).toHaveText([
    'Moneyline',
    'Run line',
    'Total',
    'First team to score',
    'First five moneyline',
    'First five total',
    'First five run line',
    'First inning moneyline',
    'First inning total',
    'First inning run line',
    'A run in the first inning',
    'Home team total',
    'Away team total',
    'Home team first five total',
    'Away team first five total',
  ])
  // A three-way market's third side reads "Tie"; the yes / no market's read "Yes" / "No".
  await expect(card.getByText('Tie', { exact: true }).first()).toBeVisible()
  await expect(card.getByText('Yes', { exact: true })).toBeVisible()
  await expect(card.getByText('No', { exact: true })).toBeVisible()
  await expect(card.getByText(/A tie is a priced outcome/).first()).toBeVisible()
})

test('the betting card asks for the signals only after the edges answer', async ({ page }) => {
  // Fired together, the two requests ran two 200-game batches at once and hit
  // nginx's 120 s limit (504). In turn, /signals reads the batch /edges cached.
  await mockAuthed(page)
  await mockGame(page)
  await mockBetting(page)
  const order: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/edges')) order.push('edges-request')
    if (r.url().includes('/signals')) order.push('signals-request')
  })
  page.on('requestfinished', (r) => {
    if (r.url().includes('/edges')) order.push('edges-done')
  })
  await page.goto('/game/745001')
  const card = page.getByRole('region', { name: 'Betting' })
  await card.getByRole('button', { name: 'Load betting' }).click()
  await expect(card.locator('section')).toHaveCount(15)
  expect(order.indexOf('edges-done')).toBeGreaterThanOrEqual(0)
  expect(order.indexOf('signals-request')).toBeGreaterThan(order.indexOf('edges-done'))
})
