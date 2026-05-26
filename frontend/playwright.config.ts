import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright config — SIM-400
 *
 * Cross-browser E2E harness. Specs in ./e2e mock the backend with `page.route`
 * so the suite runs against the built preview server with NO live API/DB — the
 * harness verifies the frontend's rendering + navigation + state handling in
 * isolation. (Full-stack E2E against a live backend is a separate live-env run.)
 *
 * `webServer` builds + serves the app on :4173; CI installs browsers via
 * `npm run e2e:install` before `npm run e2e`.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',

  use: {
    baseURL: 'http://localhost:4173',
    trace: 'on-first-retry',
  },

  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],

  webServer: {
    command: 'npm run build && npm run preview -- --port 4173',
    url: 'http://localhost:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
