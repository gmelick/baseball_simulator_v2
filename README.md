# ⚾ MLB Baseball Simulation Platform

> A production-grade, pitch-by-pitch MLB game simulator powered by Statcast data, multi-dimensional similarity engines, and stochastic sampling — with a live game dashboard and interactive managerial override UI.

<br>

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Simulation Engine](#simulation-engine)
- [Similarity Engine Suite](#similarity-engine-suite)
- [Data Pipeline](#data-pipeline)
- [API Reference](#api-reference)
- [Frontend](#frontend)
- [Tech Stack](#tech-stack)
- [Project Roadmap](#project-roadmap)
- [Getting Started](#getting-started)
- [Repository Structure](#repository-structure)

---

## Overview

This platform ingests real Statcast pitch-by-pitch data (117-column schema), constructs a suite of **11 similarity engines** across eleven player and situation dimensions, and runs a **stochastic pitch-by-pitch game simulator** capable of generating 100 independent game iterations in near-real time.

The frontend presents live game dashboards, pre-game win probability estimates, full play-by-play replay, and an interactive managerial override tool — allowing users to substitute players mid-game and instantly see how simulation outcomes shift.

**Primary use case:** Player prop prediction and betting edge validation (pitcher strikeouts, batter hits/HRs, etc.) anchored to Closing Line Value as the gold-standard metric.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Data Sources                                  │
│   MLB Stats API (REST + WebSocket)  ·  Statcast / pybaseball         │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
              ┌─────────────▼──────────────┐
              │   PostgreSQL (Operational)  │  ← Raw ingestion, live state,
              │   raw.pitches · raw.games   │    lineups, game snapshots
              │   raw.players · raw.lineups │
              └─────────────┬──────────────┘
                            │ Nightly ETL (DuckDB postgres extension)
              ┌─────────────▼──────────────┐
              │  DuckDB (Analytical Layer)  │  ← Derived metrics, pitch pools,
              │  player profiles · indices  │    similarity pre-computation
              └─────────────┬──────────────┘
                            │
        ┌───────────────────▼────────────────────┐
        │        Similarity Engine Registry       │
        │  11 engines · FAISS + KD-Tree indices   │
        └───────────────────┬────────────────────┘
                            │
        ┌───────────────────▼────────────────────┐
        │         Core Simulation Loop            │
        │  pitch-by-pitch · 8 steps · 100 iters  │
        └───────────────────┬────────────────────┘
                            │
        ┌───────────────────▼────────────────────┐
        │         FastAPI Backend + Redis         │
        │  REST endpoints · WebSocket channels    │
        └───────────────────┬────────────────────┘
                            │
        ┌───────────────────▼────────────────────┐
        │     Frontend (Day Summary + Game Page)  │
        └────────────────────────────────────────┘
```

**Hybrid database design:** PostgreSQL handles all operational/transactional concerns (raw ingestion, live game state). DuckDB attaches directly to PostgreSQL via its `postgres` extension to run bulk analytical aggregations without full data loads into Python memory. A nightly ETL job owns the DuckDB lifecycle and populates the play pool and player profile tables.

---

## Simulation Engine

The core simulation loop executes **pitch by pitch** through the following 8 steps:

### Step 1 — Steal Determination
Given the lead baserunner, current pitcher (allowing-SB model), catcher (throw-out model), and game situation, compute steal attempt probability. Scale by manager's historical green-light rate at the current leverage index.

### Step 2 — Pitch Selection
Query the pitch pool weighted by pitcher-to-pitcher, batter-to-batter, and situation-to-situation similarity scores. Sample one pitch vector (type, velocity, movement, spin, location) from the weighted distribution. Optionally condition on the previous pitch type and result for sequential modeling.

### Step 3a — Steal Outcome Resolution *(if steal attempted)*
Resolve attempt success/failure using catcher-throwing and baserunner-stealing similarity scores against historical outcomes for this matchup type on this pitch.

### Step 3b — Pitch Outcome Determination *(if no steal)*
Query the outcome pool weighted by pitch-to-pitch and batter similarity. Sample one outcome: called strike, swinging strike, foul, ball, or ball in play. If ball in play, sample batted ball parameters (exit velocity, launch angle, spray angle, batted ball type) from the weighted distribution.

### Step 4 — Fielding Resolution
Given the batted ball vector, current defensive alignment, and venue, determine: which fielder fields the ball, number of outs recorded, and whether a fielding error occurs. Home run check precedes fielding model (launch angle 25–45°, speed ≥ park-adjusted threshold).

### Step 5 — Baserunner Advancement
For each baserunner, compute mandatory advance, optional advance opportunity, and — using a physics model blended with baserunner/fielder similarity — whether an extra base attempt succeeds.

### Step 6 — State Update
Produce the next `GameState` from the resolved event. Emit a structured `PlayResult` containing pitch details, outcome type, batted ball stats, fielding resolution, baserunner movements, runs scored, outs recorded, and updated game state.

### Step 7 — Manager Decision Logic
At end of plate appearance, evaluate substitution triggers: pitcher third-time-through-order, pitch count threshold, high-leverage platoon disadvantage. Execute available moves (pitching change, pinch hitter, pinch runner, defensive replacement) checking bench/bullpen availability.

### Step 8 — Loop Control
Continue until game over. Handles inning transitions, extra innings (ghost runner rule), and deterministic RNG seeding for reproducible simulations.

**Performance target:** Single game simulation < 2 seconds · 100-game batch < 30 seconds

---

## Similarity Engine Suite

All engines share a unified interface: `engine.query(entity_features, n=50) -> List[SimilarityResult]`

| # | Engine | Method | Key Features | Status |
|---|--------|--------|--------------|--------|
| 1 | **Pitcher → Pitcher** (Command & Arsenal) | W₂ / Bures-Wasserstein on GMMs | Velocity, IVB, horizontal break, spin rate, release angle, pitch mix | ✅ Complete |
| 2 | **Batter → Batter** | RBF kernel, z-score normalized | First-pitch take rate, O-swing%, whiff%, K%, BB%, batted ball profile, platoon splits | ✅ Complete |
| 3 | **Fielder → Fielder** | RBF kernel (position-specific) | OAA components, range factor, error rate by zone, arm strength proxy | ✅ Complete |
| 4 | **Baserunner → Baserunner** (Extra Base) | RBF kernel | Extra-base rate by situation, sprint speed, stop rate, score-from-second rate | ✅ Complete |
| 5 | **Baserunner → Baserunner** (Stealing) | RBF kernel | SB attempt rate, SB success rate, lead distance proxy, sprint speed | ✅ Complete (SIM-066) |
| 6 | **Catcher → Catcher** (Throwing) | RBF kernel | Caught-stealing rate by base, arm strength proxy, pop time proxy | ✅ Complete (SIM-067/072) |
| 7 | **Pitcher → Pitcher** (Allowing SBs) | RBF kernel | Slide step mix, time to plate, SB allowed rate, pickoff attempt rate | ✅ Complete (SIM-068) |
| 8 | **Manager → Manager** | RBF kernel | PH usage rate, bullpen leverage patterns, defensive replacement freq, SB green-light rate | ✅ Complete (SIM-069) |
| 9 | **Situation → Situation** | KD-Tree, weighted Euclidean | Count, outs, inning, score differential (capped ±5), base state, leverage index | ✅ Complete (SIM-070; columnarized SIM-334) |
| 10 | **Pitch → Pitch** | FAISS flat index | Pitch type, velocity, pfx_x, IVB, release position, spin axis, extension, plate location | ✅ Complete (SIM-041) |
| 11 | **Batted Ball → Batted Ball** | FAISS flat index | Exit velocity, launch angle, spray angle, batted ball type, venue | ✅ Complete (SIM-042) |

**`SimilarityEngineRegistry`** (`similarity/registry.py`) — instantiates and caches all 11 engines with unified query interface. *(✅ Shipped, SIM-048.)*

**Key modeling decisions:**
- GMM covariances stored in **standardized space** with saved `feature_means` / `feature_stds` — prevents dominant features (spin rate) from corrupting arsenal distances
- Arsenal W₂ distances calibrated to real Statcast distributions (range ~0.5–12, median ~2.84); linear exponential `exp(-W₂ / 4.10)` used over squared exponential for distinguishable tails
- Empirical Bayes shrinkage toward league/positional average for players with < 50–100 samples
- `ProcessPoolExecutor` parallelization for GMM fitting; pitch vectors batched per season

---

## Data Pipeline

### Historical Data
- **Source:** Statcast via `pybaseball` / Baseball Savant
- **Coverage:** 2022–2024 full seasons + rolling 2025 data
- **Schema:** 117-column Statcast format — all pitch physics, batted ball stats, fielding resolution, baserunning outcomes, and full game state confirmed in sample data
- **Ingestion:** ETL script with full data validation, type enforcement, and null guards for non-contact events

### Live Data
- **Source:** MLB Stats API (REST polling + WebSocket change signal)
- **Cadence:** ~30 seconds for in-progress games
- **Ingest:** Game state, current lineup, bullpen availability, base/out/count state, live score, betting odds
- **Re-simulation trigger:** End of every plate appearance; also triggered by WebSocket pitch events

### Nightly Batch
- Pre-compute aggregated player profiles (pitch mix %, velocity percentiles, whiff/chase rates, batted ball profiles, sprint speed, arm strength)
- Sprint speed loaded from Baseball Savant CSV endpoint into `raw.sprint_speed` table
- Defensive metrics computed via `defensive_metrics_computor.py` (OAA, DP conversion, framing, blocking)
- Rebuild FAISS indices over full pitch pool (~1M+ rows) *(Phase 3)*
- Recency weighting: last 2 seasons weighted 2× vs. older data

**Caching:** Simulation results cached 60s (live games) / longer (completed). Play pool queries cached 5 min. Odds data cached 5 min.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/games/{date}` | All games for a date with state, scores, lineups |
| `GET` | `/api/games/{game_pk}` | Full game data including play-by-play |
| `GET` | `/api/games/{game_pk}/simulate` | Run 100 simulations from current (or specified) state |
| `GET` | `/api/games/{game_pk}/state/{at_bat}/{pitch}` | Game state at a specific pitch for replay |
| `GET` | `/api/games/{game_pk}/plays` | Full play-by-play log at pitch-level granularity |
| `POST` | `/api/games/{game_pk}/simulate/with_override` | Re-run simulations with a modified lineup/roster |
| `WS` | `/ws/games/{game_pk}` | Live game state push channel |

**Simulation results include:** `home_win_pct`, `away_win_pct`, `avg_home_score`, `avg_away_score`, score distribution histogram, and per-player projected stats across all iterations.

---

## Frontend

### Day Summary Page

The landing page shows all MLB games for a selected date with three distinct card states:

**In-Progress Games**
- Team records, venue, live score
- Full inning-by-inning linescore with R/H/E summary
- 100-simulation summary (avg scores + win probabilities)
- SVG baseball field graphic with all 9 defensive positions labeled, current batter, and baserunners
- Current count, outs, pitcher, and batter display
- Both teams' live box score lineups

**Completed Games**
- Final score, full linescore, W/L/Save pitcher summary
- Moneyline, spread, and total odds with winning picks highlighted

**Not Started Games**
- Starting lineups (if announced), 100-simulation win probabilities
- Moneyline, spread, and total betting odds

### Game Page

All Day Summary information plus:

- **Expanded simulation boxscore** — per-player projected stats across 100 simulations
- **Play-by-play scroll panel** — at-bat level by default, expandable to individual pitches
- **Historical replay** — click any play or pitch to rewind the full page to that game state and re-run simulations from there
- **Managerial override panel** — substitute a pitcher, pinch hitter, pinch runner, or defensive replacement and instantly see updated simulation projections

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.13 |
| Web Framework | FastAPI |
| Operational DB | PostgreSQL + async SQLAlchemy + Alembic |
| Analytical DB | DuckDB (with `postgres` extension) |
| Caching | Redis |
| ML / Similarity | scikit-learn (GMMs), FAISS, NumPy |
| Data Ingestion | pybaseball, MLB Stats API |
| Containerization | Docker / docker-compose |
| Frontend | React 18 + Vite + TypeScript (REST + typed WebSocket) |
| Monitoring | Prometheus + Grafana |

---

## Project Roadmap

The platform is organized into 7 sequential phases across ~24 weeks:

| Phase | Name | Key Output | Duration | Status |
|-------|------|------------|----------|--------|
| **1** | Data Infrastructure & Pipeline | Populated play pool DB + data API layer | 2 weeks | ✅ Complete |
| **2** | Similarity Engine Suite | 11 similarity models, fully tested | 5 weeks | ✅ Complete |
| **3** | Play Pool Architecture | Indexed, query-optimized play pool | 1 week | ✅ Complete |
| **4** | Core Simulation Loop | Full pitch-by-pitch game simulator | 4 weeks | ✅ Complete |
| **5** | Simulation Runner & Backend API | FastAPI endpoints, 100-iteration runner | 3 weeks | ✅ Complete |
| **6** | Frontend Build | Day Summary + Game pages, all UI components | 6 weeks | ✅ Complete |
| **7** | Integration, Testing & Deployment | Production-ready deployed system | 3 weeks | 🔄 In progress |

### Validation Framework
- Backtesting on held-out historical data: MAE on simulated prop distributions, calibration curves (ECE), Brier Score, log-loss
- Ablation testing: swap each similarity engine against league-average baseline to isolate signal contribution
- Chi-squared goodness-of-fit test on simulated run distributions vs. historical actuals (target: p > 0.05)
- Closing Line Value as the gold standard for betting edge validation
- Results sliced by player archetype and park to surface systematic biases

---

## Getting Started

### Prerequisites

- Python 3.13
- PostgreSQL 15+
- Docker & docker-compose
- Node.js (for frontend tooling)

### Installation

```bash
git clone https://github.com/gmelick/baseball_simulator.git
cd baseball_simulator

# Configure environment
cp .env.example .env

# Build + start all services (db, redis, app)
make dev

# Run database migrations (in a separate terminal once db is healthy)
make migrate

# Load historical Statcast data (2022–2024)
python pipeline/etl/etl_historical_loader.py --seasons 2022 2023 2024

# Load sprint speed data from Baseball Savant
python pipeline/etl/etl_sprint_speed_loader.py --seasons 2022 2023 2024

# Pre-compute player profiles and defensive metrics
python pipeline/batch/player_profile_computor.py
```

### Running Simulations

```python
from simulation.sim_loop import simulate_game
from simulation.game_state import GameState

# Simulate a full game from any starting state
result = simulate_game(state=GameState(...), seed=42)   # returns a GameSimResult

# Run 100 iterations in parallel (SIM-332 ProcessPool runner)
from simulation.batch_runner import BatchRunner
summary = BatchRunner(spec).run(n=100, base_seed=42)    # returns a GameSimSummary
print(f"Home win probability: {summary.home_win_pct:.1%}")

# Calibrated win probability + betting edge
from simulation.win_probability import win_probability     # SIM-330
from betting.clv_engine import moneyline_edge_report       # SIM-339
```

---

## Repository Structure

```
baseball_simulator/
├── pipeline/
│   ├── etl/                  # Historical Statcast ingestion + sprint speed loader
│   ├── live/                 # MLB Stats API live ingestion
│   └── batch/                # Nightly profile + defensive metrics computation
├── similarity/
│   ├── registry.py           # SimilarityEngineRegistry [✅ SIM-048]
│   ├── engines/              # 11 engines (pitcher, batter, fielder, situation, ...)
│   ├── backtesting/          # recency walk-forward + backtester (ECE/Brier/log-loss) [✅ SIM-076/220]
│   └── similarity_calibration.py  # sigma / gamma / EB calibration [✅]
├── simulation/               # [✅ Phase 4 — the simulation loop lives HERE, not simulator/]
│   ├── sim_loop.py           # StateMachine + simulate_game() (8-step loop + manager hooks)
│   ├── game_state.py         # GameState / PlayResult / ManagerContext dataclasses
│   ├── run_resolution.py     # RE24 + linear-weight run resolution
│   ├── play_pool_sampler.py  # play-pool k-NN sampler (recency-weighted)
│   ├── score_fusion.py       # cross-engine score fusion
│   ├── fingerprints.py       # real query-fingerprint derivation
│   ├── results.py            # GameSimResult / GameSimSummary / BoxScore
│   ├── win_probability.py    # calibrated win probability
│   ├── prop_distributions.py # per-prop PMFs
│   ├── snapshots.py          # field / play-by-play snapshot contracts
│   ├── batch_runner.py       # ProcessPool 100-iteration runner (+ shared-memory attach)
│   └── validation/           # chi-squared historical-replay harness
├── betting/                  # [✅ Phase 4] CLV engine (implied / de-vig / edge / EV / CLV)
├── api/                      # [✅ Phase 5]
│   ├── main.py               # FastAPI app
│   ├── routes/               # REST endpoints
│   └── websocket/            # Live game WebSocket channels
├── frontend/                 # [✅ Phase 6 — React 18 + Vite + TypeScript]
│   ├── components/           # Shared UI components
│   ├── pages/                # Day Summary + Game pages
│   └── graphics/             # SVG baseball field, linescore
├── db/
│   ├── migrations/           # Alembic migration scripts
│   ├── schemas/              # PostgreSQL + DuckDB DDL
│   └── models/               # SQLAlchemy ORM models
├── tests/
│   ├── unit/
│   ├── integration/
│   └── backtesting/
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

<div align="center">

*Built with Statcast data · Powered by similarity-based stochastic simulation*

</div>
