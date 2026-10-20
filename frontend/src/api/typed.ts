/**
 * typed.ts — the typed API surface, read from the generated OpenAPI schema.
 *
 * `npm run gen:api` (openapi-typescript) generates `schema.d.ts`. These
 * aliases name its response shapes, so a type here cannot drift from the
 * backend. New code imports its types from here.
 *
 * Regenerate after any API contract change (run the export in the app image):
 *     python scripts/export_openapi.py     # api.main.app.openapi() → frontend/openapi.json
 *     cd frontend && npm run gen:api        # openapi.json → src/api/schema.d.ts
 *
 * The hand-written clients in games.ts and betting.ts mirror these shapes;
 * they do not import them from here.
 */
import type { components } from './schema'

type Schemas = components['schemas']

// Games surface (api/routes/games*.py)
export type GameCardRow = Schemas['GameCard']
export type GameCardAggregate = Schemas['GameCardAggregateResponse']
export type Linescore = Schemas['LinescoreModel']
export type PlayByPlay = Schemas['PlayByPlayModel']
export type BoxscoreCard = Schemas['BoxscoreCardModel']
export type PropEdge = Schemas['PropEdgeResponse']

// Betting surface (api/routes/betting*.py)
export type EdgesResponse = Schemas['EdgesResponse']
export type SignalsResponse = Schemas['SignalsResponse']
export type EdgeReport = Schemas['EdgeReportModel']
export type BetSignal = Schemas['BetSignalModel']

// Data-freshness surface (api/routes/data_health.py)
export type DataFreshness = Schemas['DataFreshnessResponse']

// Auth surface (api/routes/auth.py)
export type AuthStatus = Schemas['AuthStatusResponse']
