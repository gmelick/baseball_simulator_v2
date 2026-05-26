/**
 * typed.ts — SIM-420
 *
 * Typed API surface derived from the generated OpenAPI schema (`schema.d.ts`,
 * produced by `npm run gen:api` → openapi-typescript). These aliases are the
 * single generated source of truth for response shapes; new code should import
 * its types from here so they can never silently drift from the backend.
 *
 * Regenerate after any API contract change:
 *     python scripts/export_openapi.py     # api.main.app.openapi() → frontend/openapi.json
 *     cd frontend && npm run gen:api        # openapi.json → src/api/schema.d.ts
 *
 * (The hand-written clients in games.ts / betting.ts mirror these shapes today;
 * migrating them to import directly from here is a mechanical follow-up.)
 */
import type { components } from './schema'

type Schemas = components['schemas']

// Games surface (api/routes/games.py)
export type GamesOnDate = Schemas['GamesOnDateResponse']
export type GameCardRow = Schemas['GameCard']
export type GameCardAggregate = Schemas['GameCardAggregateResponse']
export type Linescore = Schemas['LinescoreModel']
export type PlayByPlay = Schemas['PlayByPlayModel']
export type LiveGameState = Schemas['LiveGameStateResponse']
export type BoxscoreCard = Schemas['BoxscoreCardModel']
export type PropEdge = Schemas['PropEdgeResponse']
export type WithOverrideResponse = Schemas['WithOverrideResponse']
export type RosterOverride = Schemas['RosterOverride']

// Betting surface (api/routes/betting.py)
export type EdgesResponse = Schemas['EdgesResponse']
export type SignalsResponse = Schemas['SignalsResponse']
export type EdgeReport = Schemas['EdgeReportModel']
export type BetSignal = Schemas['BetSignalModel']
export type LineMovementResponse = Schemas['LineMovementResponse']

// Data-freshness surface (api/routes/data_health.py — SIM-417)
export type DataFreshness = Schemas['DataFreshnessResponse']

// Auth surface (api/routes/auth.py — SIM-389)
export type AuthStatus = Schemas['AuthStatusResponse']
