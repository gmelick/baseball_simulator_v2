# Similarity Score Visualization — Design Spec (v1: Pitcher Engine)

*Status: proposed · Owner: Product Manager (Agent 1) · Drafted: 2026-05-09*

> **Scope of this document.** Defines the v1 cross-agent build of the Similarity Score Explorer — an interactive view that answers user questions like *"Which pitchers are most similar to 2025 Paul Skenes?"* and surfaces the engine's score distribution as a navigation aid. v1 is locked to the **Pitcher → Pitcher** engine; v2 generalizes to the other engines via the same component.

---

## 1. User story

> *As a sports trader (or analyst), I want to see the full distribution of similarity scores against a query pitcher, hover any bin to preview the comps inside it, and click a bin to drill into a ranked list of those pitchers with their arsenal/command sub-score breakdown — so I can both find the obvious top comps and judge how well-separated the engine's signal is around them.*

**Why a histogram-first design (not a top-N list).** A bare top-N list answers "who's most similar?" but hides whether the top-N is *meaningfully* separated from the next 50 candidates. The histogram makes calibration health visible: a healthy engine produces a bell-shaped distribution centered near 0.5 (the project-wide median target) with a thin right tail of true comps. A spike at 0.99 means COLLAPSED diagnostic. A flat distribution means NO_SPREAD. The same component therefore serves both the *user* ("who's similar?") and the *team* ("is this engine healthy?").

---

## 2. Agent ownership map for this feature

| Slice | Primary agent | Why them | Secondary input |
|---|---|---|---|
| User story, acceptance criteria, prioritization vs. SIM-041/042 | **Product Manager** | Owns scope and definition-of-done | Betting Analyst (does this serve the trader workflow?) |
| Histogram bin design, score-distribution semantics, calibration thresholds | **ML / Modeling Engineer** | Owns engine output, knows what a "healthy" distribution looks like, defines COLLAPSED / NO_SPREAD bands | Baseball Analyst (sniff-test the surfaced comps) |
| Smell-test of comps surfaced for marquee pitchers (Skenes, deGrom, Kershaw, etc.) | **Baseball Analyst** | The primary defense against bugs that look like features | ML Engineer |
| FastAPI endpoint, response shape, Redis caching, error paths | **Backend Developer** | Owns application-layer wiring of the engine into HTTP | ML Engineer (engine warmup), Performance Engineer (cache TTL) |
| Histogram interaction design — hover preview, click drill-down panel, color coding for top-N, bin-width controls | **UX Designer** | Owns frontend interaction patterns | Baseball Analyst (which sub-scores are worth surfacing in the drill-down) |
| First-query latency budget, arsenal-cache warmup, Redis TTL choice | **Performance Engineer** | The engine docstring warns first query pays a ~1.2s lazy-cache cost; subsequent queries are <1ms. Warmup + caching is in scope for this feature | Backend Developer |
| Endpoint unit tests, frontend bin-logic tests, regression test for the JSON contract | **QA / DevOps** | Owns the test gate that prevents drift between API and frontend | Backend Developer |

**Agents *not* on this feature (and why):**
- **Data Engineer** — no schema changes; engine reads existing `derived.pitcher_season_metrics` and `raw.players`.
- **Betting Analyst** — v1 is a discovery/exploration view, not a betting signal. Wires in once the comp distribution is being used to weight prop-pricing pools.

---

## 3. API contract

**Endpoint.** `GET /api/similarity/pitcher/{pitcher_id}/{season}`

**Query params.**
- `bins` *(int, default 20)* — histogram bin count. UX may expose 10/20/40 toggles.
- `top_n` *(int, default 20)* — number of comps to flag in the top-N highlight band.

**200 response shape.**
```json
{
  "query": {
    "pitcher_id": 694973,
    "season": 2025,
    "p_throws": "R",
    "full_name": "Paul Skenes"
  },
  "engine": "pitcher_similarity",
  "engine_version": "0.1.0-phase2",
  "population_size": 2412,
  "score_summary": {
    "min": 0.04, "p25": 0.41, "median": 0.53, "p75": 0.66, "max": 0.94,
    "mean": 0.52, "std": 0.18
  },
  "bins": [
    {
      "bin_index": 0,
      "lo": 0.00, "hi": 0.05,
      "count": 7,
      "preview": [
        {"pitcher_id": 111, "season": 2024, "full_name": "Pitcher Foo", "score": 0.043}
      ],
      "members": [ /* full list of pitchers in this bin, see member shape below */ ]
    }
  ],
  "top_n": [ /* same member shape, ordered desc by score */ ],
  "diagnostic": {
    "status": "HEALTHY",   // HEALTHY | COLLAPSED | NO_SPREAD
    "median_target": 0.50,
    "median_observed": 0.53
  }
}
```

**Member shape (used in `bins[*].members`, `bins[*].preview`, and `top_n`).**
```json
{
  "pitcher_id": 594798,
  "season": 2024,
  "full_name": "Jacob deGrom",
  "p_throws": "R",
  "score": 0.91,
  "arsenal_score": 0.94,
  "command_score": 0.86,
  "sample_pitches": 1842
}
```

**Bin design.**
- Linear bins from 0.0 to 1.0. Default `bins=20` → bin width 0.05. This matches the project median target of 0.50 and gives the histogram a natural "midpoint" tick.
- Bin edges: `[lo, hi)` except the rightmost bin which is `[lo, hi]` so score=1.0 has a home.
- `preview` ⊂ `members` — the top 5 by score within the bin, used for the hover tooltip without forcing the frontend to sort the full bin client-side.

**Diagnostic logic** *(owned by ML Engineer; lives in the endpoint's response)*:
- `COLLAPSED` if ≥80% of scores fall in a single bin.
- `NO_SPREAD` if `std < 0.05` over the population.
- `HEALTHY` otherwise.

**Error paths.**
- `404 {"detail": "pitcher not in engine"}` — query (pitcher_id, season) not loaded.
- `503 {"detail": "engine warming"}` — engine present but partition not yet built (startup race).

**Caching.**
- Redis key: `simviz:pitcher:{pitcher_id}:{season}:bins={bins}:top_n={top_n}`.
- TTL: 24 hours. The engine output is deterministic for a fixed (pitcher_id, season) until the next nightly profile rebuild — long TTL is safe and Performance Engineer-approved.
- Cache invalidated on profile-rebuild via a Redis `DEL simviz:*` step in the nightly job (out of scope for v1; documented as a follow-on).

---

## 4. Frontend component spec

**Form factor.** Self-contained HTML file at `frontend/similarity_explorer.html`. No build step, no React dependency — consistent with the Phase 6 "deferred decision" on React vs. vanilla in `agent_team.md` §UX. Plotly.js loaded via CDN for the histogram.

**Layout.**
```
┌────────────────────────────────────────────────────────────────┐
│  Similarity Explorer · Pitcher → Pitcher                        │
│  Query: [Paul Skenes  (2025, R)            ▾]   bins: 10·20·40 │
├────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┃                                                             │
│   ┃          ▆▆                                                 │
│   ┃        ▆▆██▆▆                                               │
│   ┃      ▆▆██████▆▆▆▆                                           │
│   ┃    ▆▆████████████▆▆▆▆▃▃   ◄ TOP-N highlighted in green      │
│   ┃▃▃▃██████████████████████▃▃                                  │
│   └─────────────────────────────────────────────────             │
│   0.0       0.25       0.50       0.75       1.0                │
│                                                                 │
│  HEALTH: HEALTHY · median 0.53 (target 0.50) · n=2,412          │
├────────────────────────────────────────────────────────────────┤
│  [drill-down panel — appears on bin click]                      │
│  Bin 0.85–0.90 · 4 pitchers                                     │
│   • Jacob deGrom 2024  ▰▰▰▰▰▰▰▰▰▱ 0.91   arsenal 0.94 cmd 0.86  │
│   • Tyler Glasnow 2023 ▰▰▰▰▰▰▰▰▱▱ 0.88   arsenal 0.91 cmd 0.79  │
│   • …                                                            │
└────────────────────────────────────────────────────────────────┘
```

**Interactions.**
1. **Hover a bar** → tooltip shows bin range, count, and the top 5 names in the bin (from `bins[i].preview`).
2. **Click a bar** → drill-down panel below the chart populates with `bins[i].members` — full ranked list, with arsenal and command sub-scores rendered as inline mini-bars.
3. **Top-N band** — bars whose right edge is ≥ the cutoff (the `top_n`-th-highest score) are colored green; the rest are neutral gray. Makes "the top comps" pop visually without requiring a separate chart.
4. **Health badge** — colored chip in the header: green `HEALTHY`, yellow `NO_SPREAD`, red `COLLAPSED`.

**State persistence.** The bin-count toggle (10/20/40) persists in `localStorage` as `simviz.binCount`.

---

## 5. Acceptance criteria

- [ ] `GET /api/similarity/pitcher/{pitcher_id}/{season}` returns the documented JSON shape against the live engine.
- [ ] Endpoint serves a cached response in <50ms after the first call for a given pitcher.
- [ ] First (uncached) call completes in <2s end-to-end (engine arsenal-cache warmup is the dominant cost).
- [ ] 404 on unknown pitcher; 503 on engine-not-ready.
- [ ] `frontend/similarity_explorer.html` renders against the live endpoint, supports hover preview and click drill-down on every bin, and displays the diagnostic health badge.
- [ ] Unit tests cover: bin construction, top-N highlighting band, diagnostic classification, 404 path, the round-trip JSON contract.
- [ ] Baseball Analyst sign-off on the comps surfaced for at least three marquee pitchers (Skenes 2025, deGrom 2019, Kershaw 2014).
- [ ] Regression test snapshots the JSON contract — frontend won't drift away from the backend silently.

---

## 6. Out of scope (deliberately deferred)

- **v2 engine generalization** — applying the same component to batter, fielder, catcher, baserunner, manager, situation engines. Tracked as SIM-XXX (TBD).
- **Cross-engine composite view** — "show me Skenes-similar pitchers AND in their similar situations" combinations. Phase 4+ once the simulator integrates.
- **Comp pitcher's career arc** — clicking a comp opens that pitcher's full season-by-season trajectory. Nice-to-have for v1.5.
- **Backtesting overlay** — "of the top-10 comps, how did their actual H2H performance vs. similar batters match?" — this is ML Engineer's domain and belongs in a separate `/api/similarity/pitcher/.../validation` view.
- **Cache invalidation hook in the nightly profile job.** v1 ships with the 24h TTL only; the proper invalidation is a Data Engineer ticket.

---

## 7. Sequencing

1. PM signs off on this spec (this doc).
2. Backend Developer + ML Engineer pair on the endpoint + diagnostic logic.
3. UX Designer signs off on the rendered Plotly chart against synthetic JSON before live wiring.
4. Baseball Analyst smell-tests the live comps.
5. QA/DevOps adds the regression test snapshot + 80% coverage gate compliance.
6. Performance Engineer reviews the warmup + cache TTL choices in a stress run.
