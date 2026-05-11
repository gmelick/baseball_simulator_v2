# Baseball Simulator — Product Guide

*Author: Product Manager (Agent 1) · Last updated: 2026-05-06*

> **Who this guide is for.** Anyone — engineer, analyst, designer, investor, or curious newcomer — who needs to understand what this program is, why it exists, and how to use, build on, or evaluate it. **No baseball, betting, or programming background required.** Terms are defined the first time they appear, and a glossary at the end fills in the rest.

---

## 1. What is this program, in one paragraph?

The Baseball Simulator is a software platform that **simulates Major League Baseball (MLB) games one pitch at a time.** It does this thousands of times per game using a technique called **Monte Carlo simulation** (a fancy way of saying "run the same event many times with different random outcomes and see what happens on average"). The simulator pulls in real historical pitch-by-pitch data, looks at the current matchup (this pitcher, this batter, this stadium, this score, this inning), and asks: *"In the past, when situations very similar to this one came up, what tended to happen?"* It then samples one realistic outcome (a strike, a home run, a stolen base, etc.) and moves on to the next pitch — until the game ends. Run this 100 times, average the results, and you get a probability distribution: *"This team wins ~62% of the time, the starting pitcher records about 6.4 strikeouts on average."*

That output is the product. It's used to identify mispriced bets in sports betting markets — places where bookmakers' odds disagree with the simulator's estimated probabilities by enough to be profitable over the long run.

---

## 2. Who is this for, and why does it exist?

The project frames itself as a **sports trading hedge fund.** That's the mental model: we're building the kind of quantitative, data-driven decision engine that a financial trading firm would build, but for baseball betting markets instead of stocks.

The target user is a sports trader (or trading desk) who wants to:

- Decide which player props (e.g. "Pitcher X to record over 5.5 strikeouts") are mispriced by sportsbooks.
- Estimate pre-game and live win probabilities more accurately than the consensus market.
- Get a live, updating picture of how a game is unfolding and what the most probable outcomes are from any given moment forward.
- Test "what if" scenarios — *what if the manager pulls the starter now?* — and see the projection shift instantly.

**Closing Line Value (CLV)** is the gold-standard metric the product is built around. CLV measures whether the bets the system recommended were placed at better prices than the *closing* price of the market (the final price right before first pitch). Consistently beating the closing line is the most reliable evidence that a model has real edge. If our simulator's recommendations beat closing lines, we have signal. If they don't, we don't — no matter how clever the math.

---

## 3. Mental model: how the simulator "thinks"

Imagine you wanted to predict what's about to happen on the next pitch in a real game. A naive approach would be to look at season averages — *"the batter hits .280, so there's a 28% chance of a hit."* That's wildly oversimplified. A better approach is the one humans actually use:

> *"Okay, this is a left-handed power hitter facing a hard-throwing right-handed reliever, in a 2-2 count, with two outs, runners on first and second, in the 8th inning of a tied game, in a hitter-friendly park. What kinds of plate appearances in history looked like this one, and how did they end?"*

That's what the simulator does, but with math instead of intuition. It uses **similarity engines** (more on these below) to find the most comparable historical situations, then samples one realistic outcome from that comparable pool. It does this for every pitch, for every plate appearance, for nine innings (or extras), and you get one simulated game. Run it 100 times to wash out the randomness and you get a probability distribution.

The reason this works better than season averages is that it preserves **context**. Most baseball outcomes are highly context-dependent (who's pitching, who's batting, what's the count, what's the situation). Throwing context away by averaging loses signal. Preserving it — by matching on similarity — keeps it.

---

## 4. The core building blocks

There are four big pieces. Understanding them is enough to understand the whole system.

### 4.1 The data pipeline

This is where raw baseball data enters the system. It pulls from two sources:

- **Statcast** (via a tool called `pybaseball`): Every pitch thrown in MLB since 2015 is recorded with ~117 data points — pitch velocity, spin rate, where it crossed the plate, the batted ball's exit velocity and launch angle, what the fielders did, what each baserunner did. Three full seasons (2022–2024) plus the rolling current season are loaded into the database.
- **MLB Stats API**: The official live feed. During games, this provides real-time updates roughly every 30 seconds — current score, who's on base, the count, lineups, bullpen availability, betting odds.

Behind the scenes, two databases work together. **PostgreSQL** handles all the live, transactional stuff (raw ingestion, current game state, lineups). **DuckDB** is an analytical database that connects directly to PostgreSQL and runs the heavy number-crunching queries needed for similarity analysis. A nightly batch job rolls up the day's raw data into pre-computed "player profiles" so the simulator doesn't have to crunch from scratch every time.

### 4.2 The similarity engines (the IP)

This is the actual intellectual property of the platform. There are **11 similarity engines** planned, 9 of which are built. Each one answers a specific question of the form *"Which historical X is most like this current X?"*:

| # | Engine | Question it answers |
|---|--------|---------------------|
| 1 | Pitcher → Pitcher | Which historical pitchers had the most similar arsenal and command to today's starter? |
| 2 | Batter → Batter | Which historical batters have the most similar plate-discipline and contact profile? |
| 3 | Fielder → Fielder | Which historical fielders cover ground and make plays similarly? |
| 4 | Baserunner → Baserunner (extra base) | Who runs the bases with similar aggression on doubles and singles? |
| 5 | Baserunner → Baserunner (stealing) | Who attempts stolen bases with similar frequency and success? |
| 6 | Catcher → Catcher | Who frames pitches, blocks balls, and throws out runners similarly? |
| 7 | Pitcher → Pitcher (allowing SBs) | Who has a similar ability to hold runners on base? |
| 8 | Manager → Manager | Who makes lineup, bullpen, and tactical decisions similarly? |
| 9 | Situation → Situation | Which historical game situations (count, outs, score, baserunners) most resemble the current one? |
| 10 | Pitch → Pitch | Which individual historical pitches are most physically similar (velocity, spin, location)? |
| 11 | Batted Ball → Batted Ball | Which historical batted balls (exit velo, launch angle, spray) are most similar? |

Each engine uses a mathematical method appropriate to its data — Gaussian Mixture Models with Wasserstein distance for pitcher arsenals, RBF (radial basis function) kernels for most player profiles, KD-Tree for situation matching, FAISS (Facebook AI Similarity Search) for billion-row pitch databases. You don't need to know what those mean to understand the product — they're all just ways of computing "how alike are these two things." What matters is that each engine returns a **similarity score from 0 to 1** that the simulator can use as a weight.

### 4.3 The simulation loop

Once the engines exist, the simulator can run a game. Every pitch goes through 8 steps:

1. **Steal check** — Does the lead baserunner attempt a steal? (Decided by pitcher hold-rate, catcher arm, manager aggressiveness.)
2. **Pitch selection** — What pitch does the pitcher throw? (Sampled from a pool weighted by similarity to the matchup.)
3. **Outcome** — What happens to that pitch? (Strike, ball, foul, hit into play — also sampled from a similarity-weighted pool.)
4. **Fielding** — If the ball is in play, who fields it and what happens?
5. **Baserunning** — Do runners advance? Get thrown out?
6. **State update** — Update the score, outs, runners, count.
7. **Manager decisions** — End of plate appearance: pitching change? Pinch hitter? Defensive replacement?
8. **Loop control** — Move to the next pitch, or end the game.

One full game should run in **under 2 seconds**, and a batch of 100 games should run in **under 30 seconds.** That's the performance contract the simulator must meet to be useful for live, real-time updates during actual MLB games.

### 4.4 The application layer (API + frontend)

The simulator's outputs need to get to the user. That's done via:

- A **FastAPI backend** that exposes REST endpoints (e.g. `GET /api/games/{game_pk}/simulate`) and a **WebSocket** channel for live game updates pushed to the browser.
- **Redis** as a cache so we don't re-run identical simulations or re-fetch identical odds.
- A **frontend** (web UI) with two main pages:
  - A **Day Summary** page showing every MLB game on a given date, with simulated win probabilities, betting odds, and live scores.
  - A **Game Page** with detailed play-by-play, simulation projections per player, and a **managerial override** tool that lets the user say *"swap in this pinch hitter"* and instantly see how the simulation projection shifts.

The API and frontend are **planned for Phases 5 and 6** — they don't exist yet. A skeleton FastAPI app exists today (`api/main.py`) with health endpoints, but the simulation routes haven't been built.

---

## 5. Where the project stands today

The project is structured into **7 sequential phases** spanning roughly 24 weeks of work.

| Phase | What it delivers | Status |
|-------|------------------|--------|
| 1. Data Infrastructure & Pipeline | Working ETL, populated database, data API layer | ✅ Complete |
| 2. Similarity Engine Suite | All 11 similarity engines, fully tested | 🔄 In progress (9 of 11 built — 82%) |
| 3. Play Pool Architecture | Indexed, query-optimized historical pitch pool | 🔲 Not started |
| 4. Core Simulation Loop | The 8-step pitch-by-pitch simulator | 🔲 Not started |
| 5. Simulation Runner & Backend API | FastAPI endpoints, parallel 100-iteration runner | 🔲 Not started |
| 6. Frontend Build | Day Summary + Game pages | 🔲 Not started |
| 7. Integration, Testing & Deployment | Production-ready hosted system | 🔲 Not started |

**Today's reality:** Data flows in, the database is populated, and most of the similarity engines work. The simulator itself, the API, and the frontend are all unbuilt. Anyone joining today is most likely working on either the last two engines (pitch-to-pitch and batted ball), the play pool architecture, or starting the simulation loop itself.

The two remaining engines blocking the end of Phase 2 are:
- **Pitch-to-Pitch** (SIM-041) — using FAISS to find the closest historical pitches to a given pitch vector.
- **Batted Ball-to-Batted Ball** (SIM-042) — same idea, for batted balls.

Once Phase 2 is finished, the natural next step is the `SimilarityEngineRegistry` (SIM-048) — a single object that wires all 11 engines together behind a unified interface for the simulator to call.

---

## 6. The agent team — how the work is organized

The project is staffed by a **9-agent team.** Each agent is a specialized role with a defined scope. Agents can be invoked in any chat by name. Knowing who owns what saves you from asking the wrong question to the wrong specialist.

| # | Agent | Owns |
|---|-------|------|
| 1 | **Product Manager** | Requirements, user stories, phase planning, prioritization (this guide is a PM deliverable) |
| 2 | **Baseball Analyst** | Domain validation, feature selection, "does this output pass the smell test?" |
| 3 | **ML / Modeling Engineer** | The similarity engines themselves, calibration, backtesting |
| 4 | **Data Engineer** | Schema, ETL pipelines, live ingestion, nightly profile pre-computation |
| 5 | **Backend Developer** | Simulation loop wiring, FastAPI, WebSocket, Redis caching |
| 6 | **Performance Engineer** | Hitting the 2s/30s simulation SLA, FAISS tuning, vectorization |
| 7 | **UX Designer** | Wireframes, design system, frontend component specs |
| 8 | **Betting / Markets Analyst** | CLV framework, odds integration, edge identification, prop strategy |
| 9 | **QA / DevOps** | Tests, CI/CD, Docker, deployment, monitoring |

A simple rule of thumb when starting any task: **"Whose desk does this belong on?"** If it's a question about model math, it's the ML Engineer. If it's about whether the model output makes baseball sense, it's the Baseball Analyst. If it's about data flowing into the database, it's the Data Engineer. If it's about whether we should build feature A before feature B, it's the Product Manager. The full collaboration map lives in `agent_team.md`.

---

## 7. Setting it up locally

You need:

- **Python 3.11 or newer**
- **PostgreSQL 15 or newer**
- **Docker** and **docker-compose** (for the supporting services)
- **Node.js** (only when you start working on the frontend, Phase 6+)

The project ships with a `Makefile` that wraps the most common commands. The full first-time setup is:

```bash
git clone <repo>
cd baseball_simulator_v2
cp .env.example .env             # copy the env template; fill in any blanks
make dev                         # builds Docker images, starts Postgres + Redis + the API
make migrate                     # applies all pending database migrations (Alembic)
make test                        # runs the full test suite to confirm everything works
```

If `make test` exits cleanly, you have a working local environment. Then:

```bash
# Load three seasons of historical Statcast data (slow — takes a while)
python pipeline/etl/etl_historical_loader.py --seasons 2022 2023 2024

# Load Baseball Savant sprint speed data
python pipeline/etl/etl_sprint_speed_loader.py --seasons 2022 2023 2024

# Pre-compute the player profile tables the engines read from
python pipeline/batch/player_profile_computor.py
```

After this, the database is populated and any of the existing similarity engines can be queried directly.

**Common gotchas:**
- The API expects two environment variables to be set or it will refuse to start: `BASEBALL_DB_DSN` and `REDIS_URL`. Both are in `.env.example`.
- DuckDB is **not** containerized — it runs in-process inside Python. Don't look for a DuckDB Docker container; there isn't one.
- Database credentials should never be committed. The DSN comes from the `BASEBALL_DB_DSN` environment variable.

---

## 8. The everyday workflow

The team uses a few enforced conventions to keep the codebase healthy.

**Database changes.** Any schema change *must* ship with a migration:
- PostgreSQL: an Alembic migration file in `db/migrations/versions/` (numbered sequentially: `0001_…`, `0002_…`, etc.).
- DuckDB: a numbered SQL file in `db/migrations/duckdb/` plus a bump of `db/schemas/duckdb_schema_version.txt`.

This rule (SIM-084) exists because the project previously had ~15 schema-change tickets with no path to live database application. Schema changes without migrations are not accepted.

**Tests.** New similarity engines and pipeline code require unit tests. The convention is to construct engines in tests via Python's `__new__` pattern (bypassing the constructor) so tests don't require a live database. There's also a **regression gate** (SIM-147): every engine has golden-file snapshot tests that lock in exact numerical outputs. If you intentionally change an engine, you regenerate the fixtures with `python tests/regression/generate_fixtures.py --force` and commit the new baseline.

**CI.** GitHub Actions runs on every push and PR: linting (ruff), type-checking (mypy), unit tests with an 80% coverage gate on `similarity/` and `pipeline/`, the regression gate, and a Docker build sanity check. Integration tests (which spin up real Postgres and Redis via testcontainers) run weekly to keep CI fast on day-to-day pushes.

**Sprint discipline.** Work is tracked under `SIM-XXX` ticket IDs. The `CHANGES.md` file is the running changelog of completed sprints, organized by agent. When you ship something, append to `CHANGES.md` so the next person knows what changed and why.

---

## 9. How a user will eventually interact with the system

Once Phases 5 and 6 are built, here's what the day-in-the-life of a user looks like:

A trader opens the **Day Summary** page in the morning. Every MLB game scheduled for that day is shown as a card. For games that haven't started, the card shows the announced lineups (if any), 100-simulation win probabilities, and the current betting odds (moneyline, spread, total). The trader can compare the simulator's implied probability to the bookmaker's implied probability, and a green/red highlight surfaces any meaningful edge.

When games start, those cards update live. The score, the linescore (inning-by-inning grid with R/H/E), the current count and outs, and a small SVG diagram of the field showing baserunners and defensive positions all refresh roughly every 30 seconds. Win probabilities recompute at the end of every plate appearance.

Click into a single game and the **Game Page** opens. Now there's a full play-by-play scroll, expandable to pitch-level detail. Click any past pitch and the simulator rewinds the game to that moment and re-runs 100 simulations from there — useful for asking *"How locked-in was this game in the 6th inning?"* The **managerial override panel** lets the trader test counterfactuals: pull the starter, bring in the closer early, pinch-hit for the weak bat. The simulation re-runs and the projected win probabilities and player stats shift in front of you.

Underneath all of this, completed-game data feeds back into validation: did the simulator's pre-game probabilities beat the closing line? That CLV signal — measured continuously over many games — is the metric that ultimately determines whether the system is working.

---

## 10. How we know the system works (validation)

Predictions without validation are entertainment. The project has a defined validation framework:

- **Backtesting** on held-out historical data: compute Mean Absolute Error on simulated prop distributions, calibration curves (Expected Calibration Error), Brier Score, and log-loss against actual outcomes.
- **Ablation testing**: replace each similarity engine one at a time with a "league-average" baseline and measure how much accuracy drops. This isolates how much each engine actually contributes.
- **Chi-squared goodness-of-fit** on simulated run distributions vs. real historical run distributions (target: p > 0.05, meaning we can't statistically distinguish them).
- **CLV** as the gold standard for live betting performance.
- Results sliced by **player archetype** and **park** to surface systematic biases (e.g. *"the model is great on power hitters in pitcher-friendly parks but bad on contact hitters in Coors Field"*).

The QA/DevOps role owns the test suite that enforces this; the ML Engineer designs the validation experiments; the Betting Analyst signs off that the metrics are calibrated to the actual betting use case (a 1% accuracy gain that doesn't translate to CLV is academic).

---

## 11. Repository layout — where to find things

```
baseball_simulator_v2/
├── README.md                      # The technical README (more terse than this guide)
├── PRODUCT_GUIDE.md               # ← You are here
├── agent_team.md                  # Definitions of all 9 agents and how to invoke them
├── CHANGES.md                     # Running changelog of completed work, by agent and sprint
├── BACKLOG.md                     # Proposed and in-flight tickets, with acceptance criteria
├── pyproject.toml                 # Python project config: pytest, ruff, mypy
├── alembic.ini                    # Database migration config
├── docker-compose.yml             # Local dev stack: Postgres + Redis + the API
├── requirements.txt               # Runtime Python dependencies
├── requirements-dev.txt           # Dev/test dependencies
│
├── pipeline/
│   ├── etl/                       # Bulk data loaders (historical Statcast, sprint speed, opening lines)
│   ├── live/                      # Live game ingestion from MLB Stats API
│   └── batch/                     # Nightly aggregation jobs (player profiles, defensive metrics)
│
├── similarity/
│   ├── engines/                   # One file per similarity engine (9 of 11 exist today)
│   ├── similarity_calibration.py  # Shared calibration utilities
│   └── similarity_diagnostics.py  # Diagnostic runner used by all RBF engines
│
├── api/
│   └── main.py                    # FastAPI app skeleton (only health endpoints today)
│
├── db/
│   ├── migrations/                # Alembic Postgres migrations + numbered DuckDB SQL
│   ├── schemas/                   # Authoritative schema files
│   └── models/                    # SQLAlchemy ORM models
│
├── tests/
│   ├── unit/                      # Per-module unit tests (engines, pipeline)
│   ├── integration/               # testcontainers-driven integration tests (run weekly in CI)
│   └── regression/                # Golden-file regression gate for the similarity engines
│
└── .github/workflows/
    ├── ci.yml                     # Per-push lint, type-check, unit, regression, Docker build
    ├── docker-release.yml         # Push to ghcr.io on main
    └── integration-weekly.yml     # Heavyweight integration suite, Mondays 03:00 UTC
```

---

## 12. Glossary

**Alembic** — A Python tool that manages database schema changes as version-controlled migration files.

**API (Application Programming Interface)** — A defined set of endpoints other programs can call to ask this system for data.

**Backtesting** — Replaying the system against historical data and checking whether its predictions would have been right.

**CLV (Closing Line Value)** — The difference between the price you got on a bet and the final market price right before the game starts. The most reliable measure of betting skill.

**Chi-squared goodness-of-fit** — A statistical test for whether two distributions (e.g. simulated outcomes vs. real outcomes) are significantly different.

**DuckDB** — An analytical database designed for fast in-process number-crunching. Used here for the heavy aggregation queries.

**ETL (Extract, Transform, Load)** — The category of jobs that pull raw data in, clean it up, and load it into the database.

**FAISS (Facebook AI Similarity Search)** — A library for finding nearest neighbors in very large datasets very quickly. Used for the pitch-to-pitch and batted-ball engines.

**FastAPI** — A modern Python web framework. Powers the backend API.

**GMM (Gaussian Mixture Model)** — A statistical model that represents data as a mix of overlapping bell curves. Used for the pitcher arsenal engine.

**Leverage Index** — A baseball stat measuring how high-stakes the current game situation is. Late innings of a tied game = high leverage; 9th inning of a 12-run blowout = low leverage.

**Monte Carlo simulation** — Running a process many times with random outcomes and averaging the results to estimate probabilities.

**OAA (Outs Above Average)** — A defensive metric: how many outs a fielder records compared to a league-average fielder in the same situations.

**PA (Plate Appearance)** — A single trip by a batter to home plate (one batter facing one pitcher until the at-bat ends).

**PostgreSQL** — A widely-used open-source relational database. Stores all live and raw data.

**Prop bet** — A bet on a specific event within a game, such as "pitcher X records over 5.5 strikeouts." This is the platform's primary commercial focus.

**RBF kernel (Radial Basis Function)** — A mathematical function that returns "1.0" if two inputs are identical and shrinks toward 0 as they get more different. Used for most of the similarity engines.

**Redis** — An in-memory cache. Used to store recent simulation results and odds data so we don't recompute or re-fetch unnecessarily.

**SLA (Service-Level Agreement)** — A performance contract. The simulator's SLA is "single game < 2 seconds, 100-game batch < 30 seconds."

**Statcast** — MLB's official tracking system that records ~117 data points per pitch (velocity, spin, location, exit velocity, etc.). The primary data source for this project.

**WebSocket** — A two-way connection between the browser and the server that allows the server to push updates to the browser in real time. Used for live game state.

**Wasserstein (W₂) distance** — A way of measuring how different two probability distributions are. Used in the pitcher arsenal engine to compare the "shape" of two pitchers' pitch mixes.

---

## 13. If you only remember three things

1. **The product is a Monte Carlo MLB game simulator built around 11 similarity engines, designed to find mispriced bets.** Closing Line Value is the metric that ultimately decides whether it's working.
2. **We're at the end of Phase 2 of 7.** The data pipeline and 9 of 11 similarity engines are done. The simulation loop, API, and frontend are still unbuilt.
3. **Work is divided across 9 specialist agents.** Knowing who owns what (`agent_team.md`) is the fastest way to get a useful answer.
