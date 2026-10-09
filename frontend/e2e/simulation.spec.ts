import { expect, test, type Page } from '@playwright/test'

/**
 * simulation.spec.ts — SIM-519 Part E: one simulation run per game.
 *
 * The game page's Simulation card: Run queues a run, the bar polls its
 * progress, a done run feeds the projections panel (which reads the run, no
 * batch of its own), Cancel stops a run, and an unpublished lineup's 503
 * shows its countdown.
 */

const PK = 745001

const run = (over: Record<string, unknown>) => ({
  run_id: 7,
  game_pk: PK,
  status: 'queued',
  n_iterations: 100,
  progress_done: 0,
  base_seed: null,
  position_in_queue: 1,
  existing: false,
  requested_at: new Date().toISOString(),
  started_at: null,
  finished_at: null,
  error: null,
  lineup_source: 'published',
  bullpen_source: 'synthetic',
  replay_run_id: null,
  summary: null,
  ...over,
})

async function mockGamePage(page: Page): Promise<void> {
  await page.route('**/auth/me', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ authenticated: true, mode: 'session', expires_in: 3600 }) }),
  )
  await page.route(`**/api/games/${PK}/status`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        game_pk: PK, game_status: 'scheduled', game_date: '2024-08-15', season: 2024,
        home_team_name: 'Yankees', home_team_abbrev: 'NYY', away_team_name: 'Red Sox', away_team_abbrev: 'BOS',
        home_score_final: null, away_score_final: null, sim_summary: null, odds: null,
      }),
    }),
  )
  for (const sub of ['linescore', 'plays', 'live', 'card']) {
    await page.route(`**/api/games/${PK}/${sub}**`, (route) =>
      route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"none"}' }),
    )
  }
  await page.route(`**/api/betting/games/${PK}/**`, (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"none"}' }),
  )
}

test('Run queues, the bar fills, and the projections read the finished run', async ({ page }) => {
  await mockGamePage(page)
  await page.route(`**/api/games/${PK}/simulate/runs/latest`, (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"none"}' }),
  )
  let polls = 0
  await page.route(`**/api/games/${PK}/simulate/runs/7`, (route) => {
    polls += 1
    const body =
      polls === 1
        ? run({ status: 'running', progress_done: 40, position_in_queue: null })
        : run({ status: 'done', progress_done: 100, position_in_queue: null, finished_at: new Date().toISOString() })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.route(`**/api/games/${PK}/simulate`, (route) =>
    route.request().method() === 'POST'
      ? route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify(run({})) })
      : route.fallback(),
  )
  let boxscoreUrl = ''
  await page.route(`**/api/games/${PK}/boxscore**`, (route) => {
    boxscoreUrl = route.request().url()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        n_iterations: 100,
        base_seed: null,
        players: { '101': { player_id: 101, means: { H: 1.1 }, name: 'Rafael Devers', side: 'away', lineup_slot: 3 } },
      }),
    })
  })

  await page.goto(`/game/${PK}`)
  const card = page.getByRole('region', { name: 'Simulation' })
  await card.getByRole('radio', { name: /300/ }).click()
  await card.getByRole('radio', { name: /100/ }).click()
  await card.getByRole('button', { name: 'Run' }).click()
  await expect(card.getByText(/Queued · #1 in line/)).toBeVisible()
  await expect(card.getByRole('progressbar')).toBeVisible()
  await expect(card.getByText('40 / 100 games')).toBeVisible()
  await expect(card.getByText(/100 games · .* published lineup · generic bullpen/)).toBeVisible()
  await expect(card.getByRole('button', { name: 'Re-run' })).toBeVisible()
  await expect(page.getByText('Rafael Devers')).toBeVisible()
  expect(boxscoreUrl).toContain('run_id=7')
})

test('a reload resumes a running bar, and Cancel stops it', async ({ page }) => {
  await mockGamePage(page)
  let cancelled = false
  await page.route(`**/api/games/${PK}/simulate/runs/latest`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(run({ status: 'running', progress_done: 20, position_in_queue: null })) }),
  )
  await page.route(`**/api/games/${PK}/simulate/runs/7`, (route) => {
    if (route.request().method() === 'DELETE') {
      cancelled = true
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(run({ status: 'cancelled', progress_done: 20 })) })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(cancelled ? run({ status: 'cancelled', progress_done: 20 }) : run({ status: 'running', progress_done: 20, position_in_queue: null })),
    })
  })
  await page.goto(`/game/${PK}`)
  const card = page.getByRole('region', { name: 'Simulation' })
  await expect(card.getByText('20 / 100 games')).toBeVisible()
  await card.getByRole('button', { name: 'Cancel' }).click()
  await expect(card.getByText('Cancelled after 20 of 100 games.')).toBeVisible()
  expect(cancelled).toBe(true)
})

test('an unpublished lineup shows the server words and a countdown', async ({ page }) => {
  await mockGamePage(page)
  await page.route(`**/api/games/${PK}/simulate/runs/latest`, (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"none"}' }),
  )
  await page.route(`**/api/games/${PK}/simulate`, (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      headers: { 'Retry-After': '900' },
      body: JSON.stringify({ detail: 'lineup not yet published — try again closer to game time' }),
    }),
  )
  await page.goto(`/game/${PK}`)
  const card = page.getByRole('region', { name: 'Simulation' })
  await card.getByRole('button', { name: 'Run' }).click()
  await expect(card.getByRole('alert')).toContainText('lineup not yet published')
  await expect(card.getByRole('alert')).toContainText(/Try again in 9\d\d s/)
  await expect(card.getByRole('button', { name: 'Run' })).toBeDisabled()
})
