/**
 * GamePage.tsx — SIM-391 stub (full build: SIM-393)
 *
 * Placeholder route target for `/game/:gamePk` so the Day Summary game cards
 * link somewhere coherent. SIM-393 replaces this with the real Game page
 * (play-by-play, per-player sim panels, linescore + field graphic, live WS).
 */
import React from 'react'
import { Link, useParams } from 'react-router-dom'

import { Card } from '@/components/ui'

export function GamePage(): React.ReactElement {
  const { gamePk } = useParams<{ gamePk: string }>()

  return (
    <div style={{ maxWidth: 'var(--sim-max-width)', margin: '0 auto', padding: 'var(--sim-space-6) var(--sim-space-4)' }}>
      <Link to="/" style={{ color: 'var(--sim-color-interactive)', fontSize: 'var(--sim-text-sm)' }}>
        ‹ Back to slate
      </Link>
      <div style={{ marginTop: 'var(--sim-space-4)' }}>
        <Card title={`Game ${gamePk ?? ''}`}>
          <p style={{ color: 'var(--sim-color-text-muted)' }}>
            The Game page (play-by-play, per-player projections, linescore + field
            graphic, live updates) lands here in SIM-393.
          </p>
        </Card>
      </div>
    </div>
  )
}
