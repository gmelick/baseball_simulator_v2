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
 * AppShell — rendered once the AuthProvider confirms the session.
 *
 * SIM-391 (Sprint 2) wires React Router: `/` and `/date/:date` show the Day
 * Summary slate; `/game/:gamePk` shows the Game page (a stub until SIM-393).
 */
function AppShell(): React.ReactElement {
  const { authenticated, loading, logout } = useAuth()

  // Suspense-style loading splash while the /auth/me probe runs on mount.
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
          <div className="app-header__left">
            <Link
              to="/"
              className="app-header__brand"
              style={{ textDecoration: 'none', color: 'inherit', display: 'flex', alignItems: 'center', gap: 'var(--sim-space-2)' }}
            >
              <span className="app-header__logo" aria-hidden="true">⚾</span>
              <span className="app-header__title">MLB Sim Platform</span>
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
            <button
              onClick={logout}
              style={{
                background: 'none',
                border: 'none',
                color: 'rgba(255,255,255,0.7)',
                cursor: 'pointer',
                fontFamily: 'var(--sim-font-sans)',
                fontSize: 'var(--sim-text-sm)',
                padding: '0 var(--sim-space-2)',
              }}
              aria-label="Log out"
            >
              Log out
            </button>
          </nav>
        </header>

        <main className="app-main" id="main-content">
          <Routes>
            <Route path="/" element={<DaySummaryPage />} />
            <Route path="/date/:date" element={<DaySummaryPage />} />
            <Route path="/game/:gamePk" element={<GamePage />} />

            {/* SIM-439 — Data Lab (raw.pitches explorer) */}
            <Route path="/lab" element={<DataLabLayout />}>
              <Route index element={<Navigate to="/lab/summary" replace />} />
              <Route path="summary" element={<SummaryPage />} />
              <Route path="console" element={<ConsolePage />} />
              <Route path="assistant" element={<AssistantPage />} />
            </Route>

            {/* SIM-439 — Similarity Explorer */}
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
 * App — root. Wraps the entire tree in AuthProvider so every component can
 * call useAuth(). AppShell reads the auth state and renders either the login
 * page or the main application.
 */
function App(): React.ReactElement {
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  )
}

export default App
