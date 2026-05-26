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

  await expect(page.getByRole('heading', { name: 'Day Summary' })).toBeVisible()
  await expect(page.getByText('2 games')).toBeVisible()
  await expect(page.getByText('Yankees')).toBeVisible()
  await expect(page.getByText('Dodgers')).toBeVisible()
  await expect(page.getByText('FINAL')).toBeVisible()
  await expect(page.getByText('SCHEDULED')).toBeVisible()
})

test('clicking a game card navigates to the game page', async ({ page }) => {
  await mockAuthed(page)
  await mockSlate(page)
  await mockGame(page)
  await page.goto(`/date/${DATE}`)

  await page.getByRole('link', { name: /Red Sox at Yankees/i }).click()
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
  await expect(page.getByText(/No games scheduled/i)).toBeVisible()
})
