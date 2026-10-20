import React from 'react'
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes } from 'react-router-dom'

import { LoginPage } from '@/components/auth/LoginPage'
import { AuthProvider, useAuth } from '@/contexts/AuthContext'
import { AssistantPage } from '@/pages/AssistantPage'
import { ConsolePage } from '@/pages/ConsolePage'
import { DataLabLayout } from '@/pages/DataLabLayout'
import { DaySummaryPage } from '@/pages/DaySummaryPage'
import { EngineExplorerPage } from '@/pages/EngineExplorerPage'
import { EngineIndexPage } from '@/pages/EngineIndexPage'
import { GamePage } from '@/pages/GamePage'
import { PlayerDetailPage } from '@/pages/PlayerDetailPage'
import { SituationFinderPage } from '@/pages/SituationFinderPage'
import { SummaryPage } from '@/pages/SummaryPage'

/**
 * AppShell — reads the auth state from the AuthProvider. It shows a loading
 * splash during the session probe, the login page with no session, and the
 * routed app with one.
 *
 * The routes: `/` and `/date/:date` show the day slate; `/game/:gamePk` shows
 * the game page; `/lab/*` is the Data Lab; `/similarity/*` and
 * `/player/:playerId` are the Similarity Explorer.
 */
function AppShell(): React.ReactElement {
  const { authenticated, loading, logout } = useAuth()

  // A loading splash while the /auth/me probe runs on mount.
  if (loading) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight: '100vh',
          color: 'var(--sim-color-text-muted)',
          fontFamily: 'var(--sim-font-sans)',
        }}
        aria-busy="true"
        aria-label="Loading…"
      >
        Loading…
      </div>
    )
  }

  // No valid session → show the login page.
  if (!authenticated) {
    return <LoginPage />
  }

  // Valid session → main application shell with client-side routing.
  return (
    <BrowserRouter>
      <div className="app">
        <a href="#main-content" className="skip-link">
          Skip to content
        </a>
        <header className="app-header">
          <div className="app-header__bar">
            <div className="app-header__left">
              <Link to="/" className="app-header__brand">
                <span className="app-header__logo" aria-hidden="true" />
                <span className="app-header__title">Daily Diamond</span>
              </Link>
              <nav className="app-header__nav" aria-label="Primary navigation">
                <NavLink to="/" end>
                  Games
                </NavLink>
                <NavLink to="/lab">Data Lab</NavLink>
                <NavLink to="/similarity">Similarity</NavLink>
              </nav>
            </div>
            <nav className="app-header__nav" aria-label="Utility navigation">
              <button type="button" className="app-header__logout" onClick={logout} aria-label="Log out">
                Log out
              </button>
            </nav>
          </div>
          <div className="app-header__stitch" aria-hidden="true" />
        </header>

        <main className="app-main" id="main-content">
          <Routes>
            <Route path="/" element={<DaySummaryPage />} />
            <Route path="/date/:date" element={<DaySummaryPage />} />
            <Route path="/game/:gamePk" element={<GamePage />} />

            {/* The Data Lab (the raw.pitches explorer) */}
            <Route path="/lab" element={<DataLabLayout />}>
              <Route index element={<Navigate to="/lab/summary" replace />} />
              <Route path="summary" element={<SummaryPage />} />
              <Route path="console" element={<ConsolePage />} />
              <Route path="assistant" element={<AssistantPage />} />
            </Route>

            {/* The Similarity Explorer */}
            <Route path="/similarity" element={<EngineIndexPage />} />
            <Route path="/similarity/situation" element={<SituationFinderPage />} />
            <Route path="/similarity/:engine" element={<EngineExplorerPage />} />
            <Route path="/player/:playerId" element={<PlayerDetailPage />} />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}

/**
 * App — the root. It wraps the whole tree in AuthProvider, so every component
 * can call useAuth(). AppShell reads the auth state and renders either the
 * login page or the main application.
 */
function App(): React.ReactElement {
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  )
}

export default App
