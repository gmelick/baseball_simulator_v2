# ADR-001 — Frontend Framework: React + Vite

*SIM-378 · Status: **Accepted** · Date: 2026-09-02 (executed 2026-05-25)*  
*Decision-makers: UX Designer (Agent 7), Backend Developer (Agent 5), QA/DevOps (Agent 9)*

---

## Context

Phase 6 opens a greenfield frontend on a complete Python/FastAPI backend. The frontend scope
(from `agent_team.md` §7 and the Phase-6 ticket list) includes:

- **Day Summary page** — date navigation, 3-state game cards (scheduled/live/final)
- **LinescoreGraphic + BaseballFieldGraphic** — SVG components updated from live WebSocket
- **Game page** — virtualized play-by-play, pitch drill-down, per-player simulation panels;
  all panels update simultaneously when a live WS event arrives
- **Managerial override UI v2 (SIM-398)** — staged change queue + undo + 4-rule amber indicator
  system + side-by-side baseline/override result comparison; the override queue persists across
  live WS updates without losing user state
- **Boxscore + betting card + CLV time-series chart**

The deferred framework decision from `agent_team.md:238` was: "React migration vs. vanilla JS +
WebSocket extension — decided at Phase 6 kickoff based on override UI complexity."

The driving question: is the override UI complex enough to justify a reactive framework?

## Decision

**React 18 + Vite.** TypeScript throughout.

## Options evaluated

| | Vanilla JS (ES modules) | **React 18 + Vite** ← chosen | Preact + Vite |
|---|---|---|---|
| Override v2 state model | Manual DOM + closure store | React context / Zustand — native fit | Same as React |
| Live WS multi-panel update | Manual DOM fan-out | Single state update → all consumers re-render | Same as React |
| Build tooling overhead | None | Vite (half-day scaffold, once) | Same as React |
| CLV chart library | D3 (CDN) | Recharts / Victory (npm) | Preact compat wrappers |
| Virtualized play-by-play | Custom scroll | TanStack Virtual | Same as React |
| Bundle size | ~0 KB | ~45 KB gzipped | ~3 KB gzipped |
| Dev DX | Module-reload | Vite HMR (<50 ms) | Vite HMR (<50 ms) |
| TypeScript integration | Hand-rolled | First-class | First-class |
| OpenAPI typed client (SIM-420) | Manual | `openapi-typescript` + `hey-api` auto-gen | Same as React |

## Rationale

The **managerial override v2 UI (SIM-398)** is the decisive signal. It requires:

1. A persistent **staged change queue** (multiple substitutions in flight simultaneously)
2. **Undo** of any queued change without re-running the sim
3. An **amber indicator system** (4 rules: any override active / queue non-empty / undo available /
   comparison active) that must survive live WS updates without resetting
4. **Side-by-side comparison** panels (baseline vs. override result) that update as new sim results
   arrive from the WebSocket

This is a multi-component shared-state problem, not a "simple interactivity" problem. Vanilla JS
is not precluded, but it requires hand-writing a reactive store, a virtual DOM, and a reconciler —
reimplementing React at lower quality. Preact is equivalent to React for this purpose; React was
chosen for its larger ecosystem (Recharts for the CLV chart, TanStack Virtual for the play-by-play
scroll list) and broader team familiarity.

The build tooling overhead (Vite, `package.json`, a JS CI lane) is a **one-time** cost, not a
recurring one. The Vite dev-server proxy (`/api → :8000`, `/ws → ws://localhost:8000`) eliminates
CORS in development without any backend change.

## Consequences

### Positive
- Override v2 state management is React's native use case; no custom store needed
- Live WS fan-out is a single `dispatch` into a React context/store → all consumers re-render
- TypeScript + `openapi-typescript` auto-generates typed client stubs from `/openapi.json`
- Vite HMR gives sub-50 ms reload on component changes throughout Phase 6
- Standard ecosystem: chart libs, virtualized lists, a11y primitives all available via npm

### Negative / mitigations
- **Build step required:** `npm run build` produces `frontend/dist/`; FastAPI serves it via
  `StaticFiles` (dev) / nginx serves it directly (production). Documented in WORKFLOW.md.
- **JS CI lane added (SIM-379):** typecheck + lint + build on every push. Adds ~2 min to CI.
- **Node.js >= 20 required** in development and CI. Added to prerequisites in WORKFLOW.md.

## Supersedes

The "vanilla JS + WebSocket extension" placeholder in `agent_team.md:238`.  
Closes SIM-378.
