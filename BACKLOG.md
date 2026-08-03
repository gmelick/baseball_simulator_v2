# Product Backlog

*Owner: Product Manager (Agent 1) · Last updated: 2026-06-02 (SIM-432 CLOSED — calibration LIVE. SIM-430 WORKER-SCALING RESOLVED: root cause was workers FORKING from the ~6 GB engine-loaded parent [CPython refcount/GC defeats copy-on-write → ~6 GB/worker → OOM at scale]; fixed by mp_context=forkserver [workers ~6 GB→373 MB] + a 10 GB app mem_limit. n=100 /simulate 215 s→~38 s [5.6×], no OOM, 6 workers. 30 s SLA NOT fully met — throughput plateaus past ~6 workers [serial result-handling/per-game bottleneck = the remaining SIM-430 "per-game cost" work]. Earlier part-2 [densify pitcher_sim → kill the 2 GB dict] also shipped. Remaining open: SIM-430 [per-game cost / fan-out efficiency to reach 30 s]; P2 SIM-411+413+425b [one cheap play-pool rebuild]; SIM-427 [bullpen roster]; SIM-433/434/435 CODE-COMPLETE 2026-06-02 (bullpen-availability migration+ingest / manager decision model gated SIM_MANAGER OFF / historical-odds loader — all unit-tested + regression-green; the live data-runs [MLB-API roster ingest, manager enable+validation, odds backfill] are PENDING); SIM-436 [revisit perf for 30s SLA, P3 low]; SIM-429 [K/BB pull-fix + run-conversion + fuller curve; CLV unblocked once SIM-435 backfill runs]. SIM-402/406/407/408/431/432 closed.)*

# 🔍 2026-07-27 — SIM-447: sweep completeness — 4 silently-skipped games, the neutral-site venue crash, and 331 blank venue names (next free ID → SIM-448)

**How this was found.** After the 2017-2025 sweep the ledger looked clean — 6 games "failed", all
`outcome='empty'`. Those 6 turned out to need nothing (all `status='Cancelled'`; zero pitches is the
correct answer and `empty` is the SIM-441 terminal marker that stops them being retried nightly).
But **reconciling `raw.etl_game_ingest` against `raw.pitches` per season** told a different story:
four seasons had one more game in `raw.pitches` than the ledger accounted for.

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-447a | `resumable_sweep.py` recorded FAILED games as done | Bug/Ops | P1 | S | SIM-446 | ✅ **FIXED.** The wrapper appended a `game_pk` to its progress file whenever `_dispatch_game` **returned** — but that dispatcher deliberately SWALLOWS per-game exceptions (bumps `summary["failed"]`, returns) so one bad feed cannot kill a multi-hour run. So "returned" ≠ "loaded". Four games (**529440**/2018, **632924**/2021, **663023**/2022, **777962**/2025) failed, were marked complete, and were skipped by every later attempt. **The sweep reported success while those games silently kept their pre-SIM-440 rows** — the worst failure shape available: invisible, and indistinguishable from a clean run. Now gated on the `loaded` counter incrementing. |
| SIM-447b | `KeyError: 'venue'` on one-off neutral-site games | Bug/Data | P1 | M | — | ✅ **FIXED.** The underlying cause of 2 of those 4. `gd["venue"]["id"]` raised a bare `KeyError` for the two **Field of Dreams** games (632924 = 2021-08-12 NYY@CWS, 663023 = 2022-08-11 CHC@CIN): MLB ships them with **no venue anywhere** — `gameData.venue` absent AND the schedule endpoint returning `{"link": "/api/v1/venues/null"}`. New `_resolve_venue()` + a documented `_VENUE_OVERRIDES` map (venue **5445 "Field of Dreams", Dyersville IA** — present in MLB's UNFILTERED `/api/v1/venues` catalogue, missing only from the season-filtered lists, so each entry is a lookup, not a guess). **The tempting wrong fix is `teams.home.venue`:** it is always populated and makes the crash go away, but for these games it reports the home team's REGULAR park — so it would attribute a game played in an Iowa cornfield to Guaranteed Rate Field, and because `raw.pitches.venue_id` is NOT NULL with an FK to `raw.venues`, a wrong-but-real venue satisfies every constraint and would only ever surface as a quietly distorted SIM-411 park factor. An unregistered neutral site now raises `MissingVenueError` carrying the remediation steps. Fixed at **all 4 call sites** (`_fetch_game_pitches`, `_ensure_prerequisites`, `_ensure_game`, `_ensure_teams`). |
| SIM-447c | `venu_name_short` typo → 331/331 blank venue names | Bug/Data | P2 | S | — | ✅ **FIXED + DATA REPAIRED.** `dimensions.get("venu_name_short", " ")` — missing the `e` — so the key **never matched** and every row fell through to the `" "` default. **All 331 rows in `raw.venues` stored a single space.** It survived because `" "` is TRUTHY, so every `or`-style guard downstream took it for a real name. Not cosmetic: `api/routes/games.py` selects `v.venue_name` and serves it to the front end. Fixed via `_first_nonblank(venue_name_short, name, statsapi name)` — the two Savant payload shapes use different keys (`venue_name_short` on park-factors, `name` on the statcast-venue fallback). `_ensure_venue(..., force=True)` added because the row could not be deleted and reloaded (raw.pitches FK) — the INSERT always had `ON CONFLICT DO UPDATE`; only the early return stood in the way. **All 331 repaired: 0 blank, 47 distinct names.** |
| SIM-447d | `scripts/reload_games.py` — targeted corrective reload | Tooling | P2 | M | — | ✅ **DONE.** Answers "which games actually need re-running" instead of guessing. Classifies every anomaly into **STALE** (rows but no ledger row — looks loaded, isn't), **FAILED** (not-loaded but `status='Final'`), **NEVER LOADED**, and **NOT PLAYED** (reported and deliberately skipped). `--dry-run`, `--game-pk`, `--season`, `--allow-shrink`, `--refresh-venues`. Prints rows-before→after per game and ends with the per-season ledger-vs-pitches reconciliation that found all of this. |

| SIM-447e | The sweep exited COMPLETE while games were still failing | Bug/Ops | P1 | S | SIM-447a | ✅ **FIXED.** The other half of SIM-447a, exposed by the 2026 run. Once failures stopped being falsely recorded as done, they were correctly left out of the progress file — but the driver still broke out of its retry loop the moment the child printed , so nothing ever went back for them. Reaching the end of a schedule is NOT the same as loading every game:  contains per-game failures by design. **Game 824014** (2026-06-26, a Final regular-season game the schedule endpoint does return) was lost exactly this way — the 2026 sweep reported COMPLETE with 1 failure. The child now emits a machine-readable , and the driver only breaks on ; a completed-with-failures attempt retries **just** the outstanding games, and the existing zero-progress guard still stops a deterministic failure from spinning. 824014 reloaded clean on retry — transient, same family as the SIM-446 residual, not a data defect. |

**Result: ALL TEN SEASONS (2017-2026) now fully reconcile** — every season's ledger count equals its distinct
`game_pk` count in `raw.pitches`. Tests +28 (`test_sim447_venue_resolution.py` 20,
`test_sim446_sweep_streaming.py` 8); **13 mutations run across the three fixes, 12 caught, 1 test found
hollow and corrected** (`_first_nonblank`'s whitespace guard duplicated `to_str`, so the mutation was
undetectable at that layer — the test now binds the contract rather than the implementation). Gates:
unit **2,557**, regression **53**, ruff + format + mypy clean.

**⚠ STILL OPEN — 2026 was never swept.** `raw.pitches` holds **1,585 games / 465,793 rows** for 2026
with **zero** ledger rows, because the sweep command covered 2018-2025 only (2017 ran separately).
That is a full season of pre-SIM-440 parser data still live. Fix: `python scripts/resumable_sweep.py
--season 2026`.

# 💥 2026-07-27 — SIM-445 + SIM-446: the sweep-crash investigation, and the ETL HTTP transport (next free ID → SIM-447, now → SIM-448)

**The blocker:** the SIM-440/441 corrective reload sweep — the data run that every downstream
number depends on — could not finish. It died with `Fatal Python error:
_PyEval_EvalFrameDefault: Executing a cache.` (SIGSEGV / exit 139 in the container), i.e. CPython
dispatching into an inline-cache entry instead of a real instruction: the bytecode of
`_build_row_dict` being corrupted underneath the interpreter. **Stochastic** — observed failures at
games 3, 5, 66, 191, 193, 318 and 405.

**Seven hypotheses were tested and SIX were killed by direct measurement, not inference:** memory
exhaustion (flat 67 MiB, `OOMKilled=false`); numpy scalar coercion (3M iterations clean); the host's
BSOD driver (Riot Vanguard `vgk.sys`, 21 bugchecks in 45 days — real, and separately fixed, but the
crash persisted with it stopped); psycopg2-binary's bundled OpenSSL (source-built psycopg2 took the
address space from four OpenSSL objects to one — still crashed); numpy/OpenBLAS (import removed
entirely — still crashed); `charset_normalizer` (instrumented: **zero** calls, the API sends
`charset=UTF-8`); `simplejson` C speedups (forced stdlib `json` — crashed at 193). Docker/WSL2 was
excluded by reproducing the fault **natively on Windows** with no container.

⚠ **A methodological correction is recorded here on purpose:** single runs of a stochastic process
were briefly treated as valid A/B evidence, and one "clean" run turned out to have silently kept the
supposedly-crashing configuration. Nothing below rests on a short run.

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-445 | Source-build psycopg2, drop numpy from the ETL, add sweep crash probes | Bug/Ops | P1 | M | — | ✅ **DONE** (`191b151`). `psycopg2-binary` → `psycopg2` (+ `libpq-dev` builder / `libpq5` runtime) so the extension links the *system* libpq+libssl instead of bundling its own — psycopg2's own docs warn against the binary wheel in production, and it had put four OpenSSL objects in one address space. `numpy` removed from the ETL entirely (`math.atan` replaces `np.arctan` in the spray-angle calc). Two probes added: `scripts/native_sweep_probe.py` (runs the sweep outside Docker) and `scripts/resumable_sweep.py` (subprocess-per-attempt, records each committed `game_pk`, aborts on zero forward progress so a *deterministic* per-game defect can't spin forever). **Neither change fixed the crash** — both were correct on their own merits and are kept. |
| SIM-446 | ETL HTTP transport: `requests` → stdlib `urllib.request` | Bug/Perf | P1 | M | SIM-445 | ✅ **DONE.** The seventh hypothesis. Swapping the transport removed `requests`, the mypyc-compiled `charset_normalizer` (**four** resident copies — two standalone, two vendored), `_brotli` and `simplejson._speedups` from the loop in one step. That run **completed the full 2017 season: 2,468 games, 732,475 pitch rows, zero crashes.** Against a fault firing roughly every 200 games, surviving 2,468 is ~1-in-10⁵ — evidence, not variance. Shipped as a transport **seam**: `_fetch_once` dispatches on `ETL_HTTP_TRANSPORT` (default `urllib`), the retry policy sits above it unchanged (bounded retry, permanent-4xx-not-retried, `Retry-After` honoured + capped), and `requests` is imported **lazily** so the default path never loads those extensions at all. `requests.HTTPError` → `HttpError(OSError)`; `_Response` carries `.text/.json()/.header()` with case-insensitive header lookup. **Verified live, not just in tests:** clean run in the container (system CA store validates both hosts; `José Ramírez` round-trips), `doseq` list params correct, a 404 fails in 0.1 s, and the Savant HTML scrape still parses 31 venue rows. **Cost, measured:** no keep-alive and no compression — 865,506 B vs 116,619 B per feed/live (7.4×), ~18.7 GB vs ~2.5 GB over a 9-season backfill, but only ~70 ms/game (~25 min total). Accepted; adding gzip would put C-extension work back in the unexplained loop. (urllib does not *omit* `Accept-Encoding` — `http.client` sends `identity`, which forbids the server compressing, which is precisely why the absent gunzip branch is safe; a lock-step test now binds the pair.) Tests +30, **8 mutations run, 1 hollow test found and rewritten** (`RemoteDisconnected` also inherits `ConnectionResetError`, so it passed via `OSError` even with `HTTPException` removed from the transient set). Gates: unit **2,529**, regression **53**, ruff + format + mypy clean. |

**⚠ NOT claimed to be root-caused.** The mechanism is still unexplained, and one anomaly survived the
clean run: game **492011** failed once with `integer out of range`, then reloaded perfectly on demand.
Sequence exhaustion and a smallint mismatch were both ruled out (`integer`, not `smallint`; no
sequence past 1e9) — so that is a *transient bad value*, which is a corruption fingerprint rather than
a data defect. It was contained by design (per-game transaction rolled back; one retry fixed it). The
rate is reduced, not provably zero. **`scripts/resumable_sweep.py` stays in use as the belt to this
change's braces.**

# 📋 2026-07-27 — REMEDIATION PLAN + register re-status (next free ID → SIM-444, now → SIM-447)

**`docs/audit/2026-07-27-REMEDIATION-PLAN.md`** supersedes the 2026-07-23 register as the working
document. It re-states all 273 findings against master `66746df`, groups every open item by FILE, and
gives a single 10-phase order with an explicit gate calendar.

**⚠ The finding that changes the picture: `wave1-remediation` was never merged.** `git branch --merged
master` does not list it; 8 commits are unmerged. **48 of the register's 49 `FIXED-BRANCH` rows are
live production defects today** — verified in code, not inferred from the label. That includes the
whole Track C simulator batch (`C1` pitcher resurrection, live with `SIM_MANAGER=1` ON in production;
`C2` steals zeroed; `C3` phantom DP runner; `C4` ROE→out), all of Track D's profile SQL, all of
Track E's similarity/calibration work, and every Track B CLV instrument fix. **Verdict: retire that
branch as a merge candidate and cherry-pick — it is strictly worse than master in three places, and
its `player_profile_computor.py` hunks auto-merge silently into a file that would contradict itself.**

**Consequence for the CLV headline:** the ~49% beat-close figure is still produced by the
un-remediated instrument, on odds that are still ~25-35% wrong-game, with an identity win-probability,
ties scored as losses, and CLV compared across different lines. It remains evidence about the
instrument, not the model.

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-444 | Execute the 10-phase remediation plan | Epic | P1 | XL | — | 🔲 **OPEN.** Phases: 0 stop irreversible loss (forward odds capture is dead wiring; there is no backup of any datastore) → 1 build the instruments (the validation harness provably cannot exercise the flags it certified) → 2 the free fixes → 3 Track B measurement → 4 Track C simulator → 5 Track D+E data layer and the look-ahead leak → 6 odds + bullpen corrective ingests → 7 **THE BIG RUN** (migrate → sweep → recompute → artifacts → tiles, once) → 8 calibration exactly once → 9 the ≥400×≥20 validation never run → 10 terminal CLV read. ~2 weeks of expensive wall-clock across ~3 months of code work, **if the order holds**. See the plan's 16 traps — several cost days if violated. |

# 🔒 2026-07-27 — SIM-443: deferred security hardening (DEFERRED BY OWNER — do not schedule ahead of the file-by-file bug work)

*Owner decision 2026-07-27: finish the per-file defect remediation first. These are tracked here so they are not lost, NOT queued. Every item below was verified live during the SIM-442 read-only audit — they are facts about the current deployment, not theory.*

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-443a | Bind datastore ports to loopback | Security/Ops | P1 | XS | — | 🔲 **DEFERRED.** `docker-compose.yml` publishes Postgres `${DB_HOST_PORT:-5432}:5432` (line 34), Redis `${REDIS_HOST_PORT:-6379}:6379` (line 59) and the app `${APP_HOST_PORT:-8000}:8000` (line 142) on ALL host interfaces. Combined with SIM-443b this means anyone who can route to the machine reaches the database directly, bypassing every application control. **Fix:** prefix each with `127.0.0.1:`. One line each; no code change. Cheapest item in the whole backlog and it removes the entire network attack surface. |
| SIM-443b | Default credentials + superuser role | Security/Ops | P1 | S | — | 🔲 **DEFERRED.** `baseball_pass` is committed in `docker-compose.yml` (and ~16 files per the 2026-07-13 audit, item I5), and the role it authenticates is a **verified superuser**: `SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolcanlogin` returns exactly one row — `baseball_user\|t\|t\|t`. `has_table_privilege('baseball_user','raw.pitches','INSERT'/'DELETE')` → `t\|t`. So the "read-only" guarantee rests entirely on application code; the credentials themselves grant full write. **Fix:** generate real credentials, drop `rolsuper`, remove the hardcoded compose DSN override that wins over `env_file`. Extends register item **I5**. |
| SIM-443c | API is unauthenticated as deployed | Security | P1 | S | — | 🔲 **DEFERRED.** `api/auth.py:248` — `require_auth` starts with `if _is_development(): return None`, returning BEFORE it reads the cookie or the API-key header. `_is_development()` is true when `ENVIRONMENT` is unset or `development`; `.env` sets `development` and `docker-compose.yml:110` defaults to it. A second pass-through at `auth.py:251-254` means even `ENVIRONMENT=production` stays open unless `AUTH_PASSWORD` or `API_KEYS` is set — neither is configured anywhere. So every `Depends(require_auth)` is inert (`sql_runner.py:82`, `ai_assistant.py:329`, `analytics.py:39`, `games.py:1293`, betting, metrics), and three SIM-439 routers carry no gate at all (`players.py:27` + an explicit `dependencies=[]` at `:90`, `schema_introspect.py:22`, `similarity_explorer.py:54`). Rate limiting is also off — `auth.py:322` defaults `RATE_LIMIT_PER_MINUTE` to 0 and `:331` gates `enabled` on `limit > 0`. **Consequence:** full exfiltration of the ~10⁷-row `raw.pitches` corpus by anyone on the LAN, 5000 rows/request via `POST /api/sql/run` with OFFSET paging, unthrottled. Writes are still blocked (see the note below) — this is a confidentiality failure, not an integrity one. Extends register item **G5**. |
| SIM-443d | No schema allowlist in the SQL console | Security | P2 | S | — | 🔲 **DEFERRED.** `validate_read_only_sql` (`api/routes/sql_safety.py:215-268`) checks length, single-statement, leading keyword and two blocklists — there is no relation or schema restriction, and the pool is opened with no `search_path` limit. `SELECT rolname, rolpassword FROM pg_authid` needs no bypass at all: no forbidden keyword, no dangerous token, and it is a read so the read-only transaction permits it. Same for `pg_stat_activity` (other sessions' full SQL text, superuser-visible). The claim that only `raw.*` is reachable appears three times as prose with no enforcing code — `sql_safety.py:32-35` (docstring), `ai_assistant.py:119-120` (the model's system prompt; a prompt is not a control), `sql_runner.py:78-80` (the OpenAPI description users read). **Note the DuckDB half of that claim IS true** — `derived.*`/`sim.*` are a separate in-process engine, genuinely unreachable from this pool. **Fix:** allowlist `raw.*` (and the specific views the UI needs) at validation time, and set a restrictive `search_path` on the pool. |
| SIM-443e | Configure `BASEBALL_DB_RO_DSN` | Security | P2 | S | SIM-443b | 🔲 **DEFERRED.** The dedicated read-only pool is already written and wired (`api/main.py:286-293`) but the env var is commented out in `.env.example:96`, so the SQL path falls back to the superuser DSN. This is the layer that would have contained **SIM-442** — the quoted-identifier bypass gave arbitrary `pg_read_file` as superuser precisely because no GRANT-level backstop existed. **Fix:** create a `GRANT SELECT`-only role and set the var. Cheap, and it converts the validator from the boundary into genuine defence-in-depth. |
| SIM-443f | Redis unauthenticated + pickle cache | Security | P2 | M | SIM-443a | 🔲 **DEFERRED.** Register item **I6**: Redis is published to the LAN with no `requirepass` while serving pickled values the API deserializes — a poisoning → deserialization-RCE path. `SIM-443a` removes the network reach; this item is the codec fix (JSON/msgpack) plus `requirepass`. |

**What is NOT at risk (verified, so it does not need re-checking):** the front end genuinely cannot WRITE. Two independent controls hold — `async with conn.transaction(readonly=True)` at `sql_safety.py:291` is unconditional and is an engine-level guarantee (Postgres rejects writes itself, so even a writable CTE like `WITH x AS (DELETE … RETURNING 1) SELECT * FROM x` fails), and the statement-keyword blocklist cannot be smuggled past by quoting (a quoted `"insert"` is an identifier, not the INSERT command). The AI assistant shares that exact executor (`ai_assistant.py:258`), and `analytics.py` / `players.py` / `schema_introspect.py` use fixed parameterised queries with no SQL interpolation.

**Already fixed, not deferred:** **SIM-442** (quoted-identifier bypass of the dangerous-function blocklist) shipped in `66746df` — it was new SIM-439 code and a live unauthenticated arbitrary-file-read primitive, so it was not left open.

# 🐛 2026-07-27 — SIM-440 + SIM-441: ETL corrective re-ingest, the bat_hand/stand semantics correction, and the ETL hardening batch (next free ID → SIM-444; SIM-442 = the SQL-validator bypass fix, SIM-443 = the DEFERRED security cluster)

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-440 | `reload_game()` corrective re-ingest + 5 parser/validator fixes + correct the inverted `bat_hand`/`stand` semantics everywhere they are documented | Bug/Data | P1 | L | — | ✅ **CODE DONE 2026-07-27 — DATA RUN PENDING.** **(a) Repair path:** `raw.pitches` was append-only (`ON CONFLICT … DO NOTHING`) with `_batch_insert` returning `len(rows)` (rows *attempted*), so re-running `load_game` after a parser fix wrote nothing and logged success; no `DELETE FROM raw.pitches` existed anywhere. New `reload_game()` (DELETE + re-INSERT in one transaction), `_batch_insert(replace_game_pk=, allow_shrink=)` returning a **measured** count, a `ReloadShrinkError` guard that rolls back rather than replacing a game with fewer rows, and `refresh_seasons/load_date_range(reload=True)` returning `{attempted, loaded, failed, skipped, rows_written}` with contained per-game failures. `backfill_lineups_and_scores` **removed** (superseded). **(b) Parser fixes:** `isOut` read from `runner["details"]` instead of `runner["movement"]` → all three `runner_*_out_advancing` FALSE on 6.55M rows → 5 baserunner success features constant 1.0 (one weighted **0.500**); `rbi` at the runner top level instead of `details` → `rbis_on_pitch` = 0 on 6.55M rows; substitution scan re-scanned from index 0 per pitch (~4× inflation of `defensive_sub_rate_late_innings`, manager weight **0.550**); in-play gate `'X'` → `('D','E','X')` (asymmetric quality-flag deletion of untracked outs); `release_speed` band 60-102 → **50-110** in BOTH layers (Alembic **0016**) — which also finally delivers SIM-087, whose widened trigger floor had been silently overridden by the stricter Python validator since 2026-05. **(c) Semantics:** `stand` is the per-PA **resolved** side (`'S'` on **0** rows); `bat_hand` is the **roster-declared** side (`'S'` on **10.4-13.3%** of rows) — the repo documented these backwards in 13+ places, and `pull_relative_spray_angle` keyed the sign flip on `bat_hand`, NULLing every switch hitter and dropping **~1 batted ball in 8** from the production batted-ball pool. Canonical definition now lives on the `raw.pitches` columns in `db/schemas/01_postgres_schema.sql`; corrected in DuckDB migration **0014** (schema **13 → 14**), `02_duckdb_schema.sql`, SUPERSEDED headers on DuckDB 0003/0006/0007, `ai_assistant._SCHEMA_CATALOG`, 3 architecture docs, and `_build_pitch_pool`. **(d) Tests:** +44 (`test_sim440_reload_game.py`) incl. a validator↔trigger **lock-step** guard and a single-Alembic-head assertion; `test_data_engineer_sim051.py` **rewritten** (it was structurally incapable of failing — own copied SQL, own reference, own table — and enshrined the inverted premise in its test name); `test_data_engineer_sim336.py` contract re-pointed at live source; `test_sim_store.py` version 13→14 + a derive-from-latest-migration drift guard. Every fix mutation-checked. Gates: unit **2,418**, regression **53**, ruff + format + mypy clean. **⚠ NOTHING IS LIVE** until `refresh_seasons(reload=True)` runs — and a **partial** sweep is worse than none (two incompatible substitution-flag semantics in one column). Sequencing: full sweep → drive `failed` to 0 → `make profile-computor` → `make calibrate` + `validate-props --write-calibration` (the baserunner SUCCESS sub-score stops being degenerate) → rebuild engine artifacts → regenerate baserunner/manager goldens. **⚠ Alembic:** master is linear 0001→0016; `wave1-remediation` also has a `0016` off `0015` — whichever merges second must be renumbered or Alembic reports two heads and applies nothing. |
| SIM-441 | ETL hardening: 12 defects in `etl_historical_loader.py` + schema support (Alembic 0017) | Bug/Data | P2 | L | SIM-440 | ✅ **CODE DONE 2026-07-27 — DATA RUN PENDING.** **Robustness:** per-game isolation on the INCREMENTAL path with a bounded circuit breaker (`_CONSECUTIVE_FAILURE_LIMIT`, default 5) so one bad feed cannot wedge the nightly chain but an API outage still fails loudly; `nightly_ingest.sh` exits non-zero on any failure; `_ensure_venue` retry loops replaced (they never broke on success and discarded a good response if a later attempt failed); pooled `requests.Session`, no retry on permanent 4xx, `Retry-After` honoured and capped; locked pool construction; Savant scrape fails loudly (`SavantScrapeError`). **Correctness:** `load_date_range` chunked to ≤364-day slices with a coverage check + the Final gate it lacked; `_ensure_players` never fabricates handedness (skips instead), reads the feed's own person record, upserts everyone, position defaults `'UT'`; manager fallback stops installing `coaches[0]` and the closeout probe gained `season_end IS NULL` + `ORDER BY`/`LIMIT`; `season_end = NULL` on conflict is now the deliberate "returned from missed time" semantic; wind parsed from the single `weather.wind` string (both columns were NULL on every game ever loaded); spray angle NULL behind home plate; comma-stripping removed; `'C'` dropped from `GAME_TYPES`. **Audit trail:** error ledger moved INSIDE the pitch transaction and made idempotent; new `raw.etl_game_ingest` terminal outcome so a zero-pitch game stops re-running nightly forever; `_validate_row` mirrors the remaining `raw.pitches` CHECKs so a violating row skips one pitch instead of rolling back the whole game. **Schema 0017:** `field_assist_6_plus`, `raw.players.active` DROPPED, `uq_etl_errors_natural_key`, `raw.etl_game_ingest`. **Tests:** +45 (`test_sim441_etl_hardening.py`); **17 mutations run, all 17 caught** — two of the new tests were themselves found hollow during that check and rewritten. Gates: unit **2,479**, regression **53**, ruff + format + mypy clean. ⚠ Parser-value fixes are inert until `refresh_seasons(reload=True)` runs. |

# 🧪 2026-07-24 — SIM-439: Data Lab (raw.pitches explorer) + generalized Similarity Explorer (next free ID → SIM-440)

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-439 | Front-end UI to explore the backend DB: raw.pitches summary analytics + read-only SQL console + AI assistant; per-engine similarity explorer with component-score breakdown | Feature/Frontend+Backend | P2 | L | — | ✅ **DONE 2026-07-24**: built to the spec of two expert agents (a top baseball analyst deciding *what* to surface, a top UI/UX designer deciding *look & feel*), synthesized into a file-by-file plan. **Backend** (7 new `api/routes/*` + `main.py` wiring): `sql_safety.py` (the ONE read-only SELECT-only validator + executor — read-only txn, `statement_timeout`, in-SQL row cap, no `;`/write-keyword/dangerous-fn even inside a CTE), `sql_runner.py` (`POST /api/sql/run`), `analytics.py` (`/api/analytics/*` — season-bounded pitch-mix/outcomes/count-state/velo/zone/leaderboards, all `data_quality_flag=FALSE`, float8-cast), `players.py` (pg_trgm name typeahead + detail), `schema_introspect.py` (`/api/schema`), `similarity_explorer.py` (generalizes the pitcher-only route to all 8 score engines via `SCORE_ADAPTERS` mapping each engine's REAL sub-score fields/weights; honestly 404s the 3 distance engines; `/engines`·`/{engine}/meta`·`/query`·`/pair`·`/situation/query`), `ai_assistant.py` (optional, gated on `ANTHROPIC_API_KEY`; NL→read-only SQL via the SAME safe path→narrate; SSE stream; `/status` degrades gracefully). **Frontend** (React 18/Vite/TS, zero new npm deps — hand-rolled SVG charts + a `<textarea>` editor + hand-rolled MarkdownLite): Data Lab (Summary dashboard, SQL Console, AI Assistant) + Similarity (engine index, explorer with comps + ComponentScoreBars + pair-drill Radar, Situation Finder, Player Detail); nav added to the app-shell header; matches the `--sim-*` tokens. **The composite score is never shown as Σ(weight·sub-score)** — the √(min EB) confidence discount + sample size are always surfaced. Deps: `anthropic>=0.40,<1.0` (import-gated), env `ANTHROPIC_API_KEY`/`ASSISTANT_MODEL`/`BASEBALL_DB_RO_DSN`/etc. Gates green: ruff + mypy + `tests/unit/test_sql_safety.py` (41) + `test_similarity_explorer_adapters.py` (11); frontend tsc + eslint (`--max-warnings 0`) + `vite build` (110 modules). Live data requires the running Docker stack (Postgres + built engines); the AI assistant requires a key. |

# 🐛 2026-07-23 — SIM-438: live pipeline could never create a new game (next free ID → SIM-439, now → SIM-440)

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-438 | `_upsert_game_record()` omits `season` — every INSERT of an unseen game fails `NOT NULL`, silently | Bug/Data | P1 | S | — | ✅ **DONE 2026-07-23**: `raw.games.season` is `INTEGER NOT NULL` (migration 0001, never relaxed) and is half of all three composite FKs — `(venue_id, season)`→`raw.venues`, `(home/away_team_id, season)`→`raw.teams` — but `pipeline/live/live_ingestion_pipeline.py::_upsert_game_record()` never supplied it, so **every game the live pipeline had not seen before raised `NotNullViolationError` and was never created**. SILENT because the call site is fire-and-forget (`asyncio.create_task`), so the exception never propagated; the `ON CONFLICT (game_pk) DO UPDATE` path kept working for games the historical ETL had already loaded, so status transitions looked healthy the whole time. **Fix:** supply `season` from the schedule API's `game["season"]` (which arrives as a STRING, hence `int()`), falling back to the game-date year so a thin payload can never re-break a NOT NULL column. **Found by:** the 2026-07-23 weekly-integration repair, proved empirically against a migrated DB (not by inspection). +3 integration tests (`tests/integration/test_sim438_live_game_upsert.py`) covering the API-string insert, the fallback, and the ON CONFLICT status transition; mutation-checked (reverting the fix reproduces `NotNullViolationError`). **Note:** the insert still requires `raw.teams`/`raw.venues` rows to exist for that season — this fix makes the insert possible, it does not seed FK parents. |

# 🧹 2026-06-22 — SIM-437: ETL type-coercion helpers consolidated (next free ID → SIM-439)

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-437 | Consolidate the two ETL loaders' duplicate `_to_float`/`_to_int`/`_to_bool`/`_to_str` coercion helpers into one shared module | Tech-debt/Refactor | P3 | S | — | ✅ **DONE 2026-06-22**: new `pipeline/etl/coercion.py` (public `to_float`/`to_int`/`to_bool`/`to_str`, robust historical semantics incl. `NaN`→`None` — a strict SUPERSET that also closes the sprint loader's latent NaN gap). `etl_historical_loader.py` + `etl_sprint_speed_loader.py` + the unit test import from it; `import math` dropped from the historical loader. ruff + mypy clean, 158 loader tests green. Docs synced (CODE_REVIEW_CHECKLIST counts + new section, CHANGES, CLAUDE.md §5). **Left separate (different family, would be a behavior change):** `_opt_int` (`pipeline/live/bullpen_availability_ingest.py`) + `_opt_float` (`pipeline/bettingpros_odds_provider.py`) — they do NOT treat `""` as missing; flagged as a future consolidation candidate. |

# 🔭 2026-06-01 — Roadmap tickets filed (SIM-433 / SIM-434 / SIM-435, all P1)

*Filed off the SIM-430/432 session + the operator's roadmap. **Execution order:**
(1) SIM-430 worker-scaling → (2) SIM-433 bullpen availability → (3) SIM-427 + SIM-434 manager
decision model + recompute profiles → (4) full sims on the updated profiles → (5) SIM-429
calibration → (6) SIM-435 historical odds → (7) SIM-429 CLV backtest. Plans:
`docs/audit/2026-06-01-roadmap-sim430-429-411-413-425b-427.md`.*

| ID | Title | Type | Pri | Size | Depends-on | Status |
|---|---|---|---|---|---|---|
| SIM-433 | Per-game bullpen availability + IL ingestion (MLB Stats API) | Feature/Data | P1 | M | live-ingestion | 🟡 **Migration 0015 APPLIED + 2024 ingest running 2026-06-03** (`--duckdb :memory:` so the workload query needs no `/data` lock). **v2 LANDED 2026-06-03**: `build_rows` now emits one row per pitcher on each team's FULL active roster (both sides) + timeline-based rest for unused arms (`_rest_as_of`), so the available-but-UNUSED reliever signal IS captured — validated on real 2024 games (28 arms/game, 10-11 available-but-unused/game; +7 tests). REMAINING: a v2 re-ingest to replace the v1 appeared-only rows (idempotent UPSERT) + the USAGE-refinement consumption (SIM-427). The 2024 ingest + the 2017-2023/2025/2026 backfill ran as v1 (appeared-only) and should be re-run under v2. |
| SIM-434 | Manager pull + reliever-selection decision model (fatigue/rest · TTO · leverage · platoon) | Feature | P1 | L | SIM-427, SIM-433 | ✅ **ENABLED in production 2026-06-04** (`SIM_MANAGER: "1"` in docker-compose `app` env). Validated 400 sims × 3 games OFF vs ON: pitchers/game 2.0→9.25, starter IP ~9→~6.4 (realistic), **runs −0.10/team (no distortion)**, box flat — fixes the SIM-429 pitcher-K/BB over-prediction (starters no longer pitch complete games). conftest pins `SIM_MANAGER=0` so tests stay flag-off; no sim-output fixtures to regen. Uses league-flat default profile + synthetic bullpen. **Follow-on (not blocking):** pull model ~1 IP long vs MLB (tune `_DEFAULT_MANAGER_PROFILE`); wire REAL per-team SIM-427 profiles in place of the default. |
| SIM-435 | Historical odds loader (opening + closing lines) — unblock the CLV backtest | Feature/Data | P1 | M | SIM-405 odds provider | ✅ **DONE 2026-06-06**: full 2024-season backfill via the real BettingPros provider — **2,378 games** in `raw.game_odds` (14,268 rows) + `raw.prop_odds` (171,771 rows), opening+closing, game (ML/RL/total) + 7 props. Smoke-validated (closing≠opening = real line movement). ~15h network run, idempotent via odds_hash. |
| SIM-436 | Revisit /simulate per-game cost + fan-out efficiency to meet the 30s SLA | Perf | P3 (low) | M | SIM-430 | ✅ **Throughput SOLVED 2026-06-06** (single-game <30s is hardware-bound). Profiled: per-game cost is the IRREDUCIBLE per-PA full-pool scoring (~1.5–1.9s/iter × ~83 PAs); machine build is free; per-PA memoization doesn't help (situational factor varies every PA); host CORE-BOUND at ~6 → a single game can't go <30s at n=100 without fewer iters/smaller pool. **Fix:** the CLV backtest is parallelized ACROSS games (`scripts/clv_backtest.py --workers`, forkserver, byte-identical, ~373MB/worker) → ~6× → ~20–32s effective/game; n=65 gives the same CLV as n=100. |

**SIM-433** — `raw.game_lineups` records who PLAYED, not who was AVAILABLE. To compute a manager's
bullpen tendencies we must distinguish "didn't pitch = manager CHOSE not to" (signal) from "couldn't
pitch = IL/unavailable" (NOT signal). Ingest the per-game 26-man active pitching staff + IL status from
the MLB Stats API (boxscore/roster + transactions endpoints — same source the SIM-405 BettingPros
bridge + live pipeline already use), and derive recent-workload availability (days since last
appearance, pitches in last N days, back-to-back) from `raw.pitches`. New table
`raw.game_bullpen_availability(game_pk, team_id, pitcher_id, available, reason[active/IL/rest],
days_rest, pitches_last_3d)`. Feeds SIM-427's manager-profile USAGE columns (the available-but-unused
signal) — the prerequisite that makes SIM-427's roster meaningful.

**SIM-434** — the pull / replacement DECISION model the sim lacks (today `_maybe_pull_starter` sees
only pitch-count + leverage; `_pick_reliever` is crude). Build: (a) a per-pitcher fatigue/rest state in
the loop (days rest + recent pitch counts, seeded from SIM-433); (b) a times-through-the-order
effectiveness decay on the starter; (c) reliever-selection scoring over the AVAILABLE bullpen by
leverage × platoon × effectiveness × rest; (d) a pull decision combining pitch-count + TTO + leverage +
the manager's fitted pull tendency. Replaces the crude hooks; wires the manager into production (incl.
the `pitcher_pitch_count`-never-incremented bug fix) behind a `SIM_MANAGER=1` flag. ALSO fixes the
SIM-429 pitcher-K/BB over-prediction (starters finally get pulled). Distribution-shifting → regenerate
the regression golden fixtures + validate at ≥400 sims/game; gate so each effect is measured alone.

**SIM-435** — the CLV backtest (the trading-fund north star) is blocked ONLY by missing historical odds
(`raw.game_odds`/`raw.prop_odds` are empty). The hooked-up provider
(`pipeline/bettingpros_odds_provider.py`, SIM-405) exposes historical lines; extend it to read CLOSING
lines (today: opening + current only) and build a loader that backfills `raw.game_odds` +
`raw.prop_odds` for Final games with `line_type='opening'` and `'closing'` (via the SIM-370 provider
seam + the existing `odds_hash` ON CONFLICT dedup + SIM-340 `mark_closing_*` convention). Unblocks the
SIM-429 CLV backtest (entry=opening vs closing line).

**SIM-436 (P3, low)** — the SIM-430 worker-scaling fix (forkserver) cleared the OOM and got n=100
`/simulate` to ~38 s (215 s → 5.6×), but the 30 s batch SLA is NOT met and throughput **plateaus past
~6 workers** (6 ≈ 8 ≈ ~38 s) — a serial bottleneck, not a worker-count one. Revisit the per-game cost +
fan-out efficiency to land < 30 s: profile the parent-side result collection (the `GameSimResult`
un-pickle + `GameSimSummary` aggregation across 100 results) and the per-request StateMachine rebuild;
candidates include trimming/streaming the per-iteration result payload, aggregating incrementally as
futures complete, and caching the per-request machine. LOW priority — simulations already run at scale
(no OOM); this is polish to hit the stated SLA.

---

> # 🚀 PHASE 6 — Frontend Build — OPEN 2026-09-02 (audit executed 2026-05-25)
>
> **Phase 5 is fully CLOSED and CI-green on Python 3.11.15** (unit+regression **1814 pass / 1 skip / 0 fail @ 89% coverage**; 8 CI jobs; the post-close CI-stabilization — ruff 0.15.14 config, mypy, coverage measurement, and 2 py3.11 failures [a gitignored fixture + a slow-test timeout] — is done). A full **9-agent program audit + independent QA cross-validation** filed **43 Phase-6 tickets (SIM-378→SIM-420)**. **Phase 6 = the Frontend Build** — a greenfield UI on the complete backend, PLUS the API contracts the UI can't start without and the live-env / realism / hardening debt. See `docs/HANDOFF_PHASE6.md`, `docs/audit/2026-09-02-phase5-close-program-audit.md`, `docs/audit/2026-09-02-phase6-prioritized-tickets.md`.
>
> **⚠ Reality check (QA-confirmed):** the pre-existing Phase-6 tickets SIM-127–131 cite parent tickets SIM-108/109/112/122–126 that **don't exist**; `frontend/{components,graphics,pages}/` are **empty dirs**; there is no build tooling or API→UI serving path. SIM-382 backfills the chain. **Next free ID: SIM-437** (SIM-433 = per-game bullpen availability/IL ingestion, SIM-434 = manager pull/reliever decision model, SIM-435 = historical odds loader — all P1, filed 2026-06-01; SIM-436 = revisit /simulate perf for the 30s SLA [P3, low, filed 2026-06-02]; see the top banner; SIM-430 = full-pool `/simulate` throughput / 2s-30s SLA perf ticket, filed 2026-05-30 from the SIM-402 live re-measure — see the SIM-402 row: per-game ~2.2s full-pool cost + the n-iteration fan-out that's serial at 1 worker and OOM-deadlocks at 10; SIM-431 = the Python-3.13 migration (CLOSED 2026-05-30); SIM-430 per-game cost cut 1.21x (2026-05-30) + part-2 densified pitcher_sim (2 GB dict→11.2 MB shared matrix) + part-3 WORKER-SCALING RESOLVED 2026-06-02 (root cause: workers FORK from the ~6 GB engine parent, CPython refcount/GC defeats COW → ~6 GB/worker; fix = mp_context=forkserver [→373 MB/worker] + 10 GB app mem_limit; n=100 215 s→~38 s, no OOM, workers=6). 30 s SLA NOT fully met — plateaus past ~6 workers (serial result-handling/per-game bottleneck = the remaining SIM-430 work); SIM-432 = calibrator/validate_props ↔ live-schema reconciliation (CLOSED 2026-06-01 — calibration now live); SIM-422→429 = the similarity-engine-wiring epic; SIM-421 = the P3 full-market-projection enhancement).
>
> **SIM-432 (P1) — calibrator + validate_props ↔ live-schema reconciliation — ✅ CLOSED 2026-06-01. CALIBRATION IS NOW LIVE.** The SIM-406 fit + SIM-407 validate scripts were reconciled to the live (SIM-408-trimmed, all-seasons-rebuilt) schema — ground truth read from the running containers, NOT the `.sql` files — and actually run on the stack. Cascade resolution: (a) batter `xba`/`xslg` already guarded (`_opt`, ee1188f) — and the rebuilt DB in fact HAS `first_pitch_take_rate`/`max_exit_velo`/the `*_vs_r` block, so (c) was a stale finding; (b) pitcher calibrator dropped the `RESULT_FEATURES` import (removed in SIM-067), now fits `sigma_command` over the engine's 7 `COMMAND_FEATURES` behind an info_schema guard, `sigma_results` vestigial keep-default; (d) `validate_props._fetch_final_games` now selects `home_score_final`/`away_score_final`. PLUS a regression guard: 7 degenerate sub-scores were returning `calibrate_sigma`'s 1.0 (would clobber baserunner `RBF_SIGMA_SPEED=0.8171` on apply) — extended the `_fit_sigma` 0.0 keep-default sentinel (new `calibrate_sigma(degenerate_value=…)`, mostly-constant-aware) to the fielder/baserunner/manager calibrators so the report applies with ZERO silent regressions. **Ran live:** `make calibrate` (2017–2026, arsenal-sample 30000) → `/data/calibration.json` (arsenal median W₂ 2.818, ARSENAL_SCALE 4.0655, σ_command 1.078, 7 keep-default sentinels); `make validate-props` (60 games 2024 → win-prob ECE 0.171, 7 reliability anchors merged). App boot now logs `applied fitted calibration to 8 engines; win-prob map: reliability-curve(2026..2017)` (was identity). 13 new unit tests; 219 calibration/engine tests green; ruff+mypy clean. Audit: `docs/audit/2026-06-01-sim432-calibration-schema-reconciliation.md`. Follow-ups (NOT SIM-432): a fuller multi-season curve fit gated on SIM-430 throughput; pitcher K/BB prop over-prediction → SIM-429.
>
> | Tier | Tickets |
> |---|---|
> | P0 — kickoff gates (frontend foundation + API contracts) | SIM-378…SIM-390 (13) |
> | P1 — frontend build | SIM-391…SIM-401 (11) |
> | P1 — backend/perf/data prerequisites + live-env verification | SIM-402…SIM-410 (9) |
> | P2 — realism + hardening | SIM-411…SIM-420 (10) |
>
> ### Tier P0 — kickoff gates
> | ID | Title | Type | Pri | Size | Owner | Depends-on |
> |---|---|---|---|---|---|---|
> | SIM-378 | React-vs-vanilla-JS architecture decision (ADR) | Spec | P0 | S | UX Designer + Backend Developer + QA/DevOps | - | ✅ **Closed 2026-05-25** |
> | SIM-379 | Frontend scaffold + build tooling + frontend CI job | Infra | P0 | M | UX Designer + QA/DevOps | SIM-378 | ✅ **Closed 2026-05-25** |
> | SIM-380 | Design-system foundation (tokens, typography, spacing, Card/Panel/Badge) | Feature | P0 | M | UX Designer | SIM-379 | ✅ **Closed 2026-05-25** |
> | SIM-381 | API->frontend serving path (StaticFiles / nginx SPA fallback) | Gap | P0 | S | Backend Developer + UX Designer | SIM-378 | ✅ **Closed 2026-05-25** |
> | SIM-382 | Backfill 8 phantom Phase-6 parent tickets + re-map SIM-127-131 deps | Gap | P0 | M | Product Manager + UX Designer + Backend Developer | - | ✅ **Closed 2026-05-25** |
> | SIM-383 | Enrich GET /api/games/{date} with team/venue names + records | Feature | P0 | M | Backend Developer + Data Engineer | - | ✅ **Closed 2026-05-25** |
> | SIM-384 | Single game-card aggregate endpoint + status enum (scheduled/live/final) | Feature | P0 | M | Backend Developer | SIM-383 | ✅ **Closed 2026-05-26** |
> | SIM-385 | Typed + documented WebSocket event schema | Feature | P0 | M | Backend Developer | - | ✅ **Closed 2026-05-25** |
> | SIM-386 | Live in-progress game-state read path on the main API | Feature | P0 | L | Data Engineer + Backend Developer | SIM-384 | ✅ **Closed 2026-05-26** |
> | SIM-387 | Fix dead calibration wiring at the betting edge/CLV call site | Bug | P0 | S | Backend Developer + ML Engineer | - | ✅ **Closed 2026-05-25** |
> | SIM-388 | Multi-substitution override (array body) - unblocks SIM-128 | Feature | P0 | M | Backend Developer | - | ✅ **Closed 2026-05-26** |
> | SIM-389 | Enforce auth on data/expensive routes + browser session model + fix dev CORS | Security | P0 | M | Backend Developer + QA/DevOps | - | ✅ **Closed 2026-05-25** |
> | SIM-390 | Player-prop edge/signal API endpoints | Feature | P0 | L | Backend Developer + Betting Analyst | SIM-387 | ✅ **Closed 2026-05-26** |
>
> ### Tier P1 — frontend build
> | ID | Title | Type | Pri | Size | Owner | Depends-on |
> |---|---|---|---|---|---|---|
> | SIM-391 | Build Day Summary page (date nav + game-count badge + 3-state cards) | Feature | P1 | L | UX Designer + Backend Developer | SIM-380, SIM-383, SIM-384 | ✅ **Closed 2026-05-26** |
> | SIM-392 | Build LinescoreGraphic + BaseballFieldGraphic SVG | Feature | P1 | M | UX Designer + Backend Developer | SIM-380, SIM-386 | ✅ **Closed 2026-05-26** |
> | SIM-393 | Build Game page (play-by-play + pitch drill-down + per-player sim panels) | Feature | P1 | L | UX Designer + Backend Developer | SIM-391, SIM-385 | ✅ **Closed 2026-05-26** |
> | SIM-394 | Build per-player boxscore (100-iter averages + distribution views) | Feature | P1 | M | UX Designer + Betting Analyst | SIM-380, SIM-390 | ✅ **Closed 2026-05-26** |
> | SIM-395 | Build betting card surface (ML/spread/total + winning-side + +EV signal) | Feature | P1 | L | UX Designer + Betting Analyst | SIM-380, SIM-390 | ✅ **Closed 2026-05-26** |
> | SIM-396 | Build CLV / line-movement time-series chart | Feature | P1 | M | UX Designer + Betting Analyst | SIM-395 | ✅ **Closed 2026-05-26** |
> | SIM-397 | Managerial override UI - v1 single-sub (ships early) | Feature | P1 | M | UX Designer + Backend Developer | SIM-393 | ✅ **Closed 2026-05-26** |
> | SIM-398 | Managerial override UI - v2 staged queue + undo + multi-change (SIM-128 build) | Feature | P1 | L | UX Designer + Backend Developer | SIM-397, SIM-388 | ✅ **Closed 2026-05-26** |
> | SIM-399 | Frontend a11y + responsive/mobile + cross-browser gate | Gap | P1 | M | UX Designer + QA/DevOps | SIM-391 | ✅ **Closed 2026-05-26** |
> | SIM-400 | Cross-browser E2E harness (Playwright) | Test | P1 | L | QA/DevOps | SIM-379 | ✅ **Closed 2026-05-26** |
> | SIM-401 | Frontend deploy (static artifacts + nginx serving + CD to ghcr) | Infra | P1 | M | QA/DevOps + Backend Developer | SIM-381 | ✅ **Closed 2026-05-26** |
>
> ### Tier P1 — prerequisites + live-env verification
> | ID | Title | Type | Pri | Size | Owner | Depends-on |
> |---|---|---|---|---|---|---|
> | SIM-402 | Real-DB /simulate 2s/30s SLA verification on dedicated hardware | Perf | P1 | M | Performance Engineer + QA/DevOps | - | 🟡 **Code-complete 2026-05-28** — cold-worker n=10 root cause fixed: lifespan worker pre-warm (`BatchRunner.prewarm` + `production_factory.warm_worker_cache`, Barrier-synced all-worker spawn) + full-pool deriver-skip in `production_machine_factory`; +13 unit tests. Wall-clock 2s/30s SLA pending live-container re-measure. **2026-05-29 live bring-up:** fixed a `/dev/shm` overflow (`shm_size: 1gb`) + bounded the pre-warm (it was hanging startup ~22 min on the 64 MB-shm overflow); SLA re-measure still pending. **2026-05-30 — SLA re-measured live (full-pool, all-seasons engines), SIM-402 code DONE but SLA NOT met → spun off SIM-430:** the cold-worker stall is FIXED (n=10 ~500s→~20s, pre-warm validated). But the 2s/30s wall-clock SLA is not met — **1 worker (stable):** n=1 ~2.3s warm (~6.4s cold) / n=10 ~20s / n=100 ~215s (serial, full-pool ~2.2s/iter). **`SIM_RUNNER_WORKERS=10` is non-viable on this 15.5 GiB host** — a pre-warm worker OOM-kills, the ProcessPool then deadlocks, and every `/simulate` hangs (>400s); `.env` pinned to 1 here. SIM-402's *code* is correct/complete; the residual throughput gap is its own perf ticket → **SIM-430**.
> | SIM-403 | Enable real parallelism + wire shared-memory tiles into the live runner | Perf | P1 | M | Backend Developer + Performance Engineer | SIM-402 | ✅ **Closed 2026-05-28** (worker-count fix; SIM-403b shared_arrays= for full-pool path landed same day)
> | SIM-403b | Wire EngineArtifacts arrays through shared_memory (zero-copy across workers) | Perf | P1 | M | Backend Developer + Performance Engineer | SIM-403 | ✅ **Closed 2026-05-28** (extract_shared_arrays + attach_shared_views on EngineArtifacts; lifespan publishes; production_factory worker splices)
> | SIM-404 | Stress / concurrency / leak suite (100 sims x 30 concurrent games) | Test | P1 | L | QA/DevOps + Performance Engineer | SIM-403 | ✅ **Closed 2026-05-28** (5 slow-marked integration tests in `test_stress_concurrency_sim404.py`: warm-pool stability, no FD/subprocess leak, 30 concurrent /simulate, direct BatchRunner concurrency, cache-key race safety)
> | SIM-405 | Real odds-provider implementation behind the SIM-370 seam | Feature | P1 | L | Data Engineer + Betting Analyst | - | ✅ **Closed 2026-05-26** |
> | SIM-406 | Fit + persist a CalibrationReport over real data + apply to all engines | Feature | P1 | L | ML Engineer | SIM-408 | ✅ **Closed 2026-05-30** — `apply_calibration` added to all 8 similarity-score engines (was pitcher-only); `SimilarityCalibrator` extended to fit the 4 SIM-408-era engines (catcher/baserunner_steal/pitcher_steal/manager) sigmas + new `CalibrationReport` fields; boot now loads the report ONCE and applies it to every engine (`api.state.apply_calibration_to_engines`) + derives the win-prob map from it; `scripts/fit_calibration.py` + `make calibrate` + nightly hook + compose `CALIBRATION_REPORT_PATH=/data/calibration.json`; +29 unit tests (`test_ml_engines_sim406.py`); ruff+format+mypy clean; 4-dimension adversarial self-review passed (2 LOW fixes folded in: fielder DP/pivot independent per-array fallback + `_fit_sigma` degenerate→0.0 sentinel). SIM-407 (win-prob reliability-curve fit + prop-PMF validation) remains.
> | SIM-407 | Validate prop PMFs + run ablation/walk-forward over real outcomes | Validation | P1 | M | ML Engineer + Betting Analyst | SIM-406 | ✅ **Closed 2026-05-30** — new `simulation/prop_validation.py`: binary calibration metrics (ECE/Brier/log-loss/reliability) for the binary win-prob + over/under events; `fit_reliability_curve` emits the `[[pred,obs],…]` curve `CalibrationReport.reliability_curve` consumes → closes the SIM-406→407 handoff so `CalibrationMap.from_report` becomes a real monotone correction; `validate_prop_over_under` scores PMF `p_over(line)` vs realized over (sportsbook push convention); `pit_values`/`pmf_coverage` mid-PIT goodness-of-fit; `build_validation_report` aggregator + `write_reliability_curve_to_calibration_report` (writes the fitted curve into the on-disk CalibrationReport). `scripts/validate_props.py` runs it over real Final games (replays via the SIM-356 `record_game_plays` seam — same factory the API/batch runner use — pairing win-prob vs the real score AND prop PMFs vs the real per-player totals derived from `raw.pitches.events`: batter H/HR/TB, pitcher K/BB) + `make validate-props`. +35 unit tests (`test_ml_engines_sim407.py`); ruff+format+mypy clean. Two review-caught bugs fixed pre-push (never shipped): a `BatchRunner(machine_factory=…)` kwarg that doesn't exist, and a non-existent `_run_batch_results` seam — both replaced with the proven `record_game_plays` per-iteration collector; the props gate (`--no-props` now optional) was dropped once `raw.pitches.events` was confirmed sufficient. *Live follow-up:* run `make validate-props --write-calibration` on the stack to fit + persist the curve.
> | SIM-408 | DuckDB profile/pool build + provisioning for the 11-engine startup | Infra | P1 | M | Data Engineer | - | 🔴 **2026-05-29 live bring-up: only 7/11 engines build** — engine↔DuckDB schema divergence, NOT stale typos: catcher/manager query a column vocabulary the computor + `02_duckdb_schema.sql` never produced; `baserunner_steal_metrics`/`pitcher_steal_metrics`/`at_bat_situations` are never built (→ situation indexes 0 rows). Needs schema reconciliation + a DuckDB rebuild (can't verify in-sandbox). Finding: `docs/audit/2026-05-29-sim408-engine-schema-divergence.md`; turn-key reconciliation plan (per-engine column/table map + canonical-direction + rebuild checklist): `docs/audit/2026-05-29-sim408-reconciliation-plan.md`. **Safe hardening landed 2026-05-29:** situation engine now raises/skips on a zero-row index instead of registering a NaN-poisoned one. **Code-side reconciliation COMPLETE — all 5/5 engines 2026-05-29** (commits cc7fb60, ede86c7, 6b2c901, 95f5e1b): situation (`at_bat_situations`), baserunner_steal (`baserunner_steal_metrics` + JUMP trim), pitcher_steal (`pitcher_steal_metrics`, outcome-only), catcher (EXTEND 4 defensive sub-scores + new zone-framing cols, TRIM offense+exchange), manager (engine-vocabulary computor; aggression/platoon from the play stream, Usage gated NULL on SIM-427). DuckDB migration 0011 (3 new tables + catcher/manager schema), version 10→11; steal/catcher fixtures regenerated in-sandbox; 235 unit+regression tests green; ruff+mypy clean. **✅ VERIFIED LIVE 2026-05-29:** migration 0011 applied to the live DuckDB (non-destructive) + a 2024 validation rebuild populated all new tables (185,485 situations, steal/manager/catcher-zone metrics, all MLB-plausible) → **`build_all_engines: 11/11` in the serving app** (was 7/11). Two further divergences shaken out by actually running it: catcher positional-INSERT vs ALTER-appended cols (→ explicit column list) and the situation park-factor join (`pf.run_factor` → `factor_type='R'`/`regressed_factor`). **Production follow-up:** a full all-seasons `make profile-computor` rebuild (the new tables currently hold 2024 only; ~5.7h, now de-risked since the SQL is proven against real data).
> | SIM-409 | Lineup ingestion guarantee for scheduled games (SIM-338 lineage) | Bug | P1 | M | Data Engineer + Backend Developer | SIM-386 | ✅ **Closed 2026-05-28** (LineupNotIngestedError → 503 + Retry-After:900; `lineup_ready` bool on GameCard via EXISTS subquery)
> | SIM-410 | Wire the API p95 timing middleware | Improvement | P1 | S | Backend Developer + QA/DevOps | - | ✅ **Closed 2026-05-25** |
>
> ### Tier P2 — realism + hardening
> | ID | Title | Type | Pri | Size | Owner | Depends-on |
> |---|---|---|---|---|---|---|
> | SIM-411 | Park factor into the run environment | Improvement | P2 | L | Baseball Analyst + ML Engineer + Data Engineer | - | 🟡 **Plumbing + consumer landed 2026-06-03, gated `SIM_PARK_FACTOR` OFF** (migration 0012 `sim.outcome_pool.venue_id`; `GameState.park_run_factor`; `_apply_park_factor` relative out↔single nudge ordered after SIM-412; tests). **API wiring landed 2026-06-03** (`_resolve_park_run_factor` venue→`derived.park_factors` two-source lookup → `state.park_run_factor` → sim_kwargs, in `/simulate` + `/with_override`). **Data run DONE + validated 2026-06-03** (migration applied, pools+artifacts rebuilt, app restarted; park ENGAGES, direction correct — hitter @1.20 R 8.85→10.71, pitcher @0.80→8.62). REMAINING (→ SIM-429 calibration): the pitcher-side asymmetry (single→out pool ~3× smaller than out→single → ~5–8× too weak; `_PARK_FACTOR_STRENGTH` was cap-bound at the tested factors, so UNVALIDATED — re-run at 1.08/0.92) + a symmetry regression. Audit: `docs/audit/2026-06-03-sim411-413-425b-validation.md`.
> | SIM-412 | Home-field run advantage in the score distribution | Improvement | P2 | M | Baseball Analyst | SIM-411 | ✅ **Closed 2026-05-28** (`_apply_home_field_bias` flips HOME batted-ball outs to singles at default 0.025 rate, calibrated to MLB ~.535-.540 home_win_pct; env override `SIM_HOME_FIELD_BIAS`)
> | SIM-413 | Pitcher throwing-hand -> batter platoon split in the batted-ball matchup | Improvement | P2 | M | Baseball Analyst + ML Engineer | - | 🟡 **Plumbing + consumer landed 2026-06-03, gated `SIM_BB_PLATOON` OFF** (`p_throws` exported into the BB artifact; `battedball_new_pa(pitcher_throws=…)` soft same/opposite-hand reweight using the live `state.throw_hand`; tests). Fully production-wired (throw_hand already flows). **Data run DONE 2026-06-03** (BB artifact carries `p_throws` 100%; the reweight engages — 10,056/sweep). REMAINING (→ SIM-429): a platoon *effect* counter (the call-counter doesn't prove a draw shifted) + ≥400-sim seed-paired calibration of `platoon_off_weight`.
> | SIM-414 | W/L/S + ER + per-runner R cross-surface reconciliation | Bug | P2 | M | Baseball Analyst + Backend Developer | SIM-384 | ✅ **Closed 2026-05-28** (sub-5-IP starter rule + inning-reconstruction unearned runs + walk-forced per-runner R)
> | SIM-415 | Pagination / payload-trim for heavy endpoints | Improvement | P2 | M | Backend Developer | - | ✅ **Closed 2026-05-26** |
> | SIM-416 | App-level exception handler + structured error envelope | Improvement | P2 | S | Backend Developer | - | ✅ **Closed 2026-05-26** |
> | SIM-417 | Data-freshness/health API surface for the UI | Feature | P2 | S | Data Engineer | - | ✅ **Closed 2026-05-26** |
> | SIM-418 | Split slow tests into a dedicated CI lane | Chore | P2 | S | QA/DevOps | - | ✅ **Closed 2026-05-26** |
> | SIM-419 | Harden DuckDB profile-rebuild index recreate | Reliability | P2 | S | Data Engineer | - | ✅ **Closed 2026-05-26** |
> | SIM-420 | OpenAPI typed-client generation for the frontend | Improvement | P2 | S | Backend Developer + QA/DevOps | SIM-383, SIM-385 | ✅ **Closed 2026-05-26** |
>
> ### Tier P3 — future enhancements (post-stabilization, non-essential)
> | ID | Title | Type | Pri | Size | Owner | Depends-on |
> |---|---|---|---|---|---|---|
> | SIM-421 | Extend prop/market projection to the full book-offered market set (individual hit types — 1B/2B/3B; SB; hits-allowed/walks-allowed; H+R+RBI combos; team totals; first-five-innings ML/RL/total; NRFI/YRFI) | Feature | P3 | L | ML Engineer + Baseball Analyst + Betting Analyst | SIM-402, SIM-406, SIM-407 | 🔵 **Open** |
>
> ### EPIC — Similarity-engine wiring + full-pool scoring (SIM-422→429)
> *Design: `docs/architecture/2026-09-03-engine-wiring-and-full-pool-scoring.md`. **Audit finding:** the live sim actively uses only **2 of 11** engines (Pitch + Batted Ball, both hard-filtered); production builds the StateMachine with `manager=None`/`sim=None`/default resolver, so step 1 (manager) + step 4 (steal) are inert and steps 3/5/6 use proxies/Retrosheet constants. This epic wires **all 11 engines** into the loop via **full-pool similarity-weighted draws** — the only hard filter is the batter's hand; pitcher hand self-zeroes via the pitcher engine; NO top-K. Fork-safe via nightly disk artifacts (generalizes the SIM-421 deriver-from-disk pattern). Supersedes the SIM-300 per-pitcher play-pool tiling + the interim realism work (jitter/advancement constants) that was provisionally tagged SIM-421 in code (⚠ collides with the P3 prop-markets ticket — reconcile that tag when committing the realism work).*
>
> | ID | Title | Type | Pri | Size | Owner | Depends-on |
> |---|---|---|---|---|---|---|
> | SIM-422 | Fork-safe engine-artifact bundle + per-worker loader (full pools, pitcher×pitcher sim, batter/catcher/fielder/baserunner embeddings, situation KDTree, manager profiles) | Infra | P1 | L | Data Engineer + ML Engineer | - |
> | SIM-423 | Full-pool resident sampler + factorized-weight alias draw + **perf gate vs 2s/30s SLA** | Infra | P1 | L | Performance Engineer + Backend Developer | SIM-422 |
> | SIM-424 | Pitch full-pool scoring — steps 2/3 (Situation, Pitcher, Batter, Pitch, Catcher-recv); delete the SIM-421 jitter | Feature | P1 | L | ML Engineer + Backend Developer | SIM-423 |
> | SIM-425 | Engine-backed PlayResolver — steps 5/6 (Batted Ball + Fielder RBF + Baserunner extra-base + Situation); replace the Retrosheet advancement constants — **MOSTLY DONE**: engine-backed extra-base advancement (per-runner attempt-rates) + productive-out advancement (sac-fly tag-up / ground-out) closed the run gap (R 4.02→4.54 vs 4.62). REMAINING: Fielder RBF (out/hit/error by defensive quality). **SIM-425b plumbing + consumer landed 2026-06-03, gated `SIM_FIELDER_RBF` OFF** (migration 0012 `fielder_player_id` + `fielded_by_position` export + `player_id:position:season` fielder-embedding key fix; `last_battedball_fielder`/`fielder_quality` accessors; `_fielder_rbf_nudge` single↔out flip by the live-vs-pool OAA delta from the per-position `GameState.home/away_defense` maps; tests). v1 = single↔out (reach-on-error is a refinement). **API wiring landed 2026-06-03** (`build_game_state` fills `GameState.home/away_defense` name-keyed maps; `_sim_kwargs_from_state` passes them). **Data run DONE + validated 2026-06-03**: uncovered + fixed a pre-existing `build_team_defense_map` bug (name-format `position_code` routed through the number-keyed map → only the pitcher resolved → fielder-RBF AND SIM-428 inert in prod); post-fix the nudge ENGAGES (140 flips/sweep); also fixed the `q_pool` game-season survivorship filter (now scores the pool fielder at its own season). **ENABLED in production 2026-06-04** (`SIM_PARK_FACTOR`/`SIM_BB_PLATOON`/`SIM_FIELDER_RBF`="1" in docker-compose; seed-paired off-vs-on validated, 4 games × 150 iters: total runs +0.05 = no distortion, home-win-% 0.567→0.523 toward MLB ~0.535; conftest pins them off so the env never leaks into tests). REMAINING (→ SIM-429): seed-paired ≥400-sim×≥20-game magnitude calibration, cap/per-OAA re-derivation from §10 run-values, reach-on-error. Audit: `docs/audit/2026-06-03-sim411-413-425b-validation.md` | Feature | P1 | L | ML Engineer + Backend Developer | SIM-422, SIM-423 |
> | SIM-426 | Steal path — steps 1b/4 (Baserunner-steal + Catcher-steal + Pitcher-steal + Situation + Manager) — **DONE (v1)**: manager-independent steal decision from the runner's sb_attempt_rate/sb_success_rate; SB 0.59 / CS 0.17 / attempts 0.77 / success 0.78 all ~MLB; `cs` added to box. Catcher-arm/pitcher-hold factors are the SIM-428-adjacent follow-on (need catcher_id threaded) | Feature | P1 | M | Backend Developer + Baseball Analyst | SIM-422 |
> | SIM-427 | Engine-backed manager decisions — step 1 (Manager + Situation + Pitcher + Batter) — **BLOCKED (data dep)**: the pre-pitch/end-of-PA manager hooks exist but the most impactful decision (starter pull -> bullpen) needs a per-(team,season) bullpen roster with starter/reliever roles + the manager-metrics rebuild (task #32). No roster source in `derived.*` today (pitcher_season_metrics has no team/role/GS); must be built from raw Statcast. A pitching change with no real reliever profile is hollow in the similarity engine, so deferred rather than shipped hollow. **USAGE UN-GATED 2026-06-03**: `_compute_manager_profiles` now derives the 6 USAGE columns from `raw.pitches` pitcher-stints (fielding-manager attribution; no roster table needed) — validated read-only on 2024 (33 managers, all populated, baseball-realistic + differentiating; CHANGES.md). **CAPSTONE LANDED 2026-06-03** (`available_reliever_usage_rate`): `_compute_manager_profiles` now also computes usage normalized by OPPORTUNITY — relievers used / (used + available-but-held) from the SIM-433-v2 table (bounded [0,1], starter-free; `bullpen_opp` CTE; DuckDB migration 0013, v12→13). Validated read-only on partial v2 data (avg 0.40, held ~6 arms/game). **DEPLOYED 2026-06-03**: v2 ingest complete (21,612 games / 649,685 rows), migration 0013 applied + `_compute_manager_profiles` recomputed all 10 seasons (305 qualifying manager-seasons, **100% non-NULL coverage**, avg 0.385, range 0.227–0.75); wired as the **7th manager-engine USAGE feature (weight 0.55** — chosen via a 3-lens design panel [analyst 0.60 / ML 0.55 / red-team 0.30, the last invalidated by the 100%-coverage data run] + 2 adversarial wiring audits); golden fixtures regenerated; recalibrated live (**`sigma_usage=1.030`**, a genuine 7-feature fit, was the 1.0 default; win-prob curve restored to SIM-432 parity); boot `11/11` engines clean. **The USAGE-similarity capstone is COMPLETE** — the engine-backed pull→bullpen DECISION is the broader SIM-434 scope (gated `SIM_MANAGER` OFF). | Feature | P1 | M | Backend Developer + Baseball Analyst + Data Engineer | SIM-422, manager-metrics rebuild |
> | SIM-428 | Catcher framing in the ball/called-strike resolution — step 3 — **DONE (code) + NOW LIVE IN PROD 2026-06-03**: `_apply_framing` nudges taken pitches by the catcher's centred framing delta. ⚠ It was **silently INERT in production** until 2026-06-03 — the SIM-425b defense-map bug meant the catcher never resolved on real (name-format) lineup data. The fix activates it; it is now gated `SIM_FRAMING` (default ON, `=0` restores the pre-fix catcher-inert path for byte-identical/seeded-reproducibility). ⚠ Activation changes the flag-off RNG stream (seeded games won't reproduce pre-fix; baseline R 8.85→8.65). "Aggregate-neutral" still to be confirmed on a ≥400-sim batch (→ SIM-429). | Improvement | P2 | M | ML Engineer + Baseball Analyst | SIM-424 |
> | SIM-429 | Re-calibration + CLV backtest over the rewired loop — **MOSTLY DONE**: full-pool flipped on as the production default; rate stats within ~4% of MLB (H/HR/BB/K) at 400 sims. Run-conversion investigated: a global advancement-calib knob (`SIM_RUN_CALIB`, neutral 1.0) was found to be the WRONG lever (closes runs only by making baserunning unrealistic — second-to-home is already MLB-realistic at 1.0); residual ~12% run gap attributed to hit-sequencing / batted-ball-with-RISP. **CLV BACKTEST BUILT + RUN 2026-06-06** (`scripts/clv_backtest.py`, SIM-435 odds now loaded): sim → model prices → opening/closing → CLV per market/side, trust-labeled; parallelized across games (SIM-436). **First result (120 games): ~49% beat-close = NO demonstrable edge yet** (stable n=65/n=100; trustworthy markets — moneyline ECE 0.047 + batter H/HR/TB — all ≤50%); full-season run executing. REMAINING: the model isn't beating the sharp close, so DEVELOP an edge — close the ~10–12% run-conversion gap (hit-sequencing/RISP) + sharpen the per-bet edge estimates + get pitcher K/BB bet-grade (ECE 0.22→<0.10); the SIM-411/413/425b magnitude calibration + wiring real per-team SIM-427 manager profiles into SIM-434 fold in here. | Validation | P1 | L | ML Engineer + Betting Analyst | SIM-424, SIM-425, SIM-426, SIM-427 |
>
> **Sprint plan (6 wks):** **S1** kickoff gates SIM-378–390 · **S2** live read + cards + linescore/field (386, 391, 392) + backend 388/390 · **S3** game page + boxscore (393, 394, 420) · **S4** betting surface + override v1 (395, 396, 397) + data/ML track 405/406/407 · **S5** override v2 + perf/verification (398, 402, 403, 404, 408, 409) · **S6** a11y/Playwright/deploy + hardening (399, 400, 401, 410–419) + staging bring-up.
>
> **Critical path:** SIM-378 → 379/380/381 + 382/383/384/385/387/389 → 386 → 391/392 → 393/394 → 395/396/397 → 398. The data/ML/perf prerequisite track (402–409) runs alongside and must be **live-env verified** before the numbers it backs reach users.
>
> ---
>
> # 🏁 PHASE 5 — Backend API & Simulation Runner — COMPLETE 2026-05-24
>
> **All 28 Phase-5 tickets (SIM-350→377) + the SIM-315 carryover closed across 6 sprints.** The greenfield
> `api/` layer now serves the full surface — games/simulate/with_override/plays/state/linescore/decisions/
> boxscore/card, `/api/betting` (edges/signals/line-movement/clv), `/ws/games/{game_pk}`, `/api/odds/*`,
> `/api/similarity/*`, `/metrics`, `/health`, `/ready` — behind auth/rate-limit/CORS, a persistent ProcessPool
> runner, Redis caching, durable sim/snapshot/game-card persistence, server-side calibration + 11 engines, an
> nginx reverse proxy, and Prometheus/Grafana monitoring.
>
> | Sprint | Tier | Tickets |
> |---|---|---|
> | 1 | P0 gates | SIM-350/351/352/353/354 + 375/376/377 + 315 |
> | 2 | P1 endpoints + persistence | SIM-355/356/357/358/359 |
> | 3 | P1 lifecycle | SIM-360/361 |
> | 4 | P2 loop outputs | SIM-362/363/364/365/366 |
> | 5 | Betting surface | SIM-367/368/369/370 + `/api/betting` |
> | 6 | Testing / infra | SIM-371/372/373/374 |
>
> **Tests: 1870 unit+regression / 0 failed** (1506 at Phase-5 entry → 1870) + a 12-test E2E suite + the
> `/simulate` perf bench. DuckDB schema **v10** / Alembic head **0014**. CI: 8 jobs (incl. e2e + file-integrity);
> perf-weekly hard-gates the SLA. **Next free ID: SIM-378.**
>
> **Next: Phase 6 — Frontend Build.** Recommended entry: a 9-agent program audit → `docs/HANDOFF_PHASE6.md`
> (UX wireframes/design-system + the React-vs-vanilla decision, building on the now-complete API contract).
> **Live-environment verification debt (code-complete, mock/unit-verified — confirm on a staging bring-up):** the
> `/simulate` 2s/30s SLA over the real DB-backed factory (SIM-372), the 11-engine build (needs DuckDB profiles),
> the replay/card endpoints (`REPLAY_PERSISTENCE_ENABLED=true` + a writable replay DuckDB), a fitted
> `CalibrationReport` (SIM-220), a real odds provider behind the SIM-370 seam, and a full `docker compose up` of
> the nginx + app + monitoring stack.
>
> ---
>
> # 🚀 Phase 5 — Sprint 5 (Betting Surface) — CLOSED 2026-05-24
>
> **Fifth Phase-5 sprint shipped: the betting surface (4 tickets + the betting API).** Run-line/spread edge,
> CLV/line-movement time-series, +EV bet signals, the real-odds-provider swap seam, and a new `/api/betting`
> router. Log: `docs/SPRINT_2026-08-19_phase5_betting_surface.md`.
>
> | Ticket | Type | Status |
> |---|---|---|
> | SIM-367 run-line/spread EdgeReport from score-margin arrays (`clv_engine`) | Gap | ✅ Closed |
> | SIM-368 CLV/line-movement time-series (`betting/line_movement.py`, from `raw.game_odds`) | Gap | ✅ Closed |
> | SIM-369 bet-signal/+EV recommendations (`betting/bet_signal.py`, fractional Kelly) | Feature | ✅ Closed |
> | SIM-370 real odds/prop provider swap behind MockOddsAPI (`pipeline/odds_provider.py`) | Feature | ✅ Closed |
> | Betting API: `api/routes/betting.py` — `/edges`, `/signals`, `/line-movement`, `/clv` | Feature | ✅ Closed |
>
> **Tests: 1861 unit+regression passing / 0 failed** (1780 Sprint-4 baseline + 81 new). DuckDB schema **v10** /
> Alembic head **0014** (unchanged). **Next free ID: SIM-378.**
> **Remaining Phase 5 (the last tier — testing/infra):** SIM-371 (API/WebSocket/historical-replay E2E suite),
> SIM-372 (`/simulate` 2s/30s SLA perf gate), SIM-373 (nginx reverse proxy + dev/staging/prod env configs),
> SIM-374 (Prometheus + Grafana monitoring). After these, **Phase 5 is complete** → Phase 6 (Frontend Build).
> **Live caveats:** `/simulate` SLA, 11-engine build, replay/card endpoints, fitted calibration curve, real odds
> provider all verify in a live environment.
>
> ---
>
> # 🚀 Phase 5 — Sprint 4 (P2 Loop Outputs) — CLOSED 2026-05-24
>
> **Fourth Phase-5 sprint shipped: the loop-output gaps the frontend game cards need (5 tickets).**
> Per-inning linescore + R/H/E, the 9 fielders in the field graphic, winning/losing/save pitchers, richer
> boxscore (2B/3B/R/SB + pitcher H/R) with exact total bases, and the per-player 100-iteration boxscore-average
> API. Most outputs DERIVE from the recorded PlayResult stream (only SIM-365 touched `sim_loop.py`). Log:
> `docs/SPRINT_2026-08-12_phase5_p2_loop_outputs.md`.
>
> | Ticket | Type | Status |
> |---|---|---|
> | SIM-362 per-inning linescore + team R/H/E (`simulation/linescore.py`) | Gap | ✅ Closed |
> | SIM-363 per-position fielders → FieldSnapshot 9 slots (`build_defense_map`) | Gap | ✅ Closed |
> | SIM-364 winning/losing/save pitcher attribution (`simulation/pitcher_decisions.py`) | Gap | ✅ Closed |
> | SIM-365 extend PlayerStatLine (2B/3B/R/SB + pitcher H/R) + exact prop-TB | Improvement | ✅ Closed |
> | SIM-366 boxscore-card API (PropDistributionSet means) + `/linescore`/`/decisions`/`/boxscore` | Feature | ✅ Closed |
>
> **Tests: 1780 unit+regression passing / 0 failed** (1702 Sprint-3 baseline + 78 new). DuckDB schema **v10**
> (migration 0010 `sim.game_cards`) / Alembic head **0014**. **Next free ID: SIM-378.**
> **Remaining Phase 5:** betting surface — SIM-367 (run-line/spread EdgeReport), SIM-368 (CLV/line-movement),
> SIM-369 (bet-signal/+EV endpoint), SIM-370 (real odds provider); then testing/infra — SIM-371 (E2E/WS suite),
> SIM-372 (`/simulate` SLA gate), SIM-373 (nginx), SIM-374 (Prometheus/Grafana). **Live-DB caveats:** `/simulate`
> SLA, 11-engine build, replay/card endpoints over real data verify in a live environment.
>
> ---
>
> # 🚀 Phase 5 — Sprint 3 (P1 Lifecycle) — CLOSED 2026-05-24 · **P1 TIER COMPLETE**
>
> **Third Phase-5 sprint shipped: the two P1 lifecycle tickets — Phase 5 P1 (SIM-355→361) is now complete.**
> The long-lived API has a persistent ProcessPool/shared-memory runner and server-side calibration + all-11-engine
> startup. Log: `docs/SPRINT_2026-08-05_phase5_p1_lifecycle.md`.
>
> | Ticket | Type | Status |
> |---|---|---|
> | SIM-360 persistent `ProcessPoolExecutor` reuse + `app.state.sim_runner` + `/simulate` reuse | Perf | ✅ Closed |
> | SIM-361 `CalibrationReport` JSON persistence + startup load → `CalibrationMap` + all-11-engine build | Feature | ✅ Closed |
>
> **Tests: 1702 unit+regression passing / 0 failed** (1661 Sprint-2 baseline + 41 new). DuckDB schema **v9** /
> Alembic head **0014** (unchanged this sprint). **Next free ID: SIM-378.**
> **Phase 5 P1 (SIM-355→361) ✅ COMPLETE** — endpoints + persistence + caching + pool lifecycle + calibration serving.
> **Next (P2):** loop-output gaps SIM-362 (per-inning R/H/E), SIM-363 (fielders), SIM-364 (W/L/S), SIM-365 (richer
> boxscore + prop-TB fix), SIM-366 (boxscore-avg API) — these touch `sim_loop.py` (top truncation risk); then betting
> surface SIM-367–370 + testing/infra SIM-371–374. **Live-DB caveats:** `/simulate` SLA (→ SIM-372), 11-engine build,
> replay endpoints, and a fitted calibration curve all verify in a real environment.
>
> ---
>
> # 🚀 Phase 5 — Sprint 2 (P1 Endpoints + Persistence) — CLOSED 2026-05-24
>
> **Second Phase-5 sprint shipped: the core REST surface (5 tickets).** `/api/games/{date}`, the
> 100-iteration `/simulate`, `/simulate/with_override`, `/plays`, and `/state/{at_bat}/{pitch}` are live,
> backed by durable sim-result + pitch-snapshot persistence and Redis TTL caching. Log:
> `docs/SPRINT_2026-07-29_phase5_p1_endpoints.md`.
>
> | Ticket | Type | Status |
> |---|---|---|
> | SIM-355 `GET /api/games/{date}` + `GET /{game_pk}/simulate` (100-iter runner) | Feature | ✅ Closed |
> | SIM-356 sim-result + pitch-snapshot persistence (Alembic 0014 / DuckDB v8) | Feature | ✅ Closed |
> | SIM-357 `GET /{game_pk}/plays` + `/state/{at_bat}/{pitch}` + record→persist (DuckDB v9) | Feature | ✅ Closed |
> | SIM-358 `POST /{game_pk}/simulate/with_override` → `OverrideDelta` | Feature | ✅ Closed |
> | SIM-359 Redis TTL caching (sim 60s / listing 300s) | Feature | ✅ Closed |
>
> **Tests: 1661 unit+regression passing / 0 failed** (1603 Sprint-1 baseline + 58 new). DuckDB schema
> **v9** (migrations 0008 play_stream + 0009 state_snapshots); Postgres Alembic head **0014** (`sim.sim_runs`).
> **Next free ID: SIM-378.** Remaining P1 (Sprint 3): SIM-360 (persistent ProcessPool + shared-memory) +
> SIM-361 (CalibrationReport serving + 11-engine startup build). Then P2: loop-output gaps (SIM-362–365)
> + betting surface (SIM-367–370). **Live-DB caveats:** `/simulate` SLA over the production factory (→ SIM-372)
> and the replay endpoints (`REPLAY_PERSISTENCE_ENABLED=true` + a dedicated replay DuckDB) verify in a real env.
>
> ---
>
> # 🚀 Phase 5 — Sprint 1 (P0 Gates) — CLOSED 2026-05-24
>
> **First Phase-5 sprint shipped: all 5 P0 gates + 3 ⚠ hygiene bugs + the SIM-315 carryover (9 tickets).**
> The `api/` layer is no longer greenfield — the serialization contract, auth baseline, real
> `machine_factory`, lineup resolver, and mounted router/pipeline skeleton are in place, unblocking the
> P1 endpoint tickets. Log: `docs/SPRINT_2026-07-22_phase5_p0_gates.md`.
>
> | Ticket | Type | Status |
> |---|---|---|
> | SIM-350 serialization contract (`api/serialization.py` + `api/schemas.py`) | Spec | ✅ Closed |
> | SIM-351 auth + rate-limit + CORS baseline (`api/auth.py`) | Feature | ✅ Closed |
> | SIM-352 production DB-backed `machine_factory` | Feature | ✅ Closed (live-DB acceptance → SIM-372) |
> | SIM-353 runtime lineup/sub resolver (the SIM-338 gap) | Feature | ✅ Closed |
> | SIM-354 mount `ws_router`/`odds_router` + gated live pipeline | Gap | ✅ Closed |
> | SIM-375 ⚠ docker-compose `./simulation` mount + dead `simulator/` removed | Bug | ✅ Closed |
> | SIM-376 ⚠ `api/` added to coverage gate | Bug | ✅ Closed |
> | SIM-377 ⚠ `GameSpec._hit_rate` TypeError | Bug | ✅ Closed |
> | SIM-315 ⚠ file-integrity guard (OneDrive truncation, Option B) | Infra | ✅ Closed |
>
> **Tests: 1603 unit+regression passing / 0 failed** (1506 baseline + 97 new). Schema **v7** / Alembic
> **0013** (unchanged). **Next free ID: SIM-378.** Next up (P1, the endpoints): SIM-355 → SIM-356 →
> SIM-357/SIM-358, + SIM-359/360/361. SIM-315 is now **Closed** (the durable guard; the physical move was
> already done).
>
> ---
>
> # 🏁 PHASE 4 — Core Simulation Loop — CLOSED 2026-05-24
>
> **Phase 4 complete across 5 sprints (SIM-310→349 + SIM-220).** The simulator runs full games
> end-to-end: loop → cross-engine fusion → validation spine → output contracts → perf
> mechanisms → betting chain → manager + situational decisions. **All six ⚠ audit live bugs
> fixed.**
>
> | Layer | Status |
> |---|---|
> | Sim loop | `simulation/sim_loop.py` — 8-step loop, `simulate_game()`, manager + situational decisions |
> | Outputs | GameSimSummary (win%/raw arrays/CIs), per-player BoxScore, win-prob, prop PMFs, field/PBP snapshots |
> | Perf | ProcessPool 100-iter runner + shared-memory attach (≤2 GB); Bench 4/5 + weekly CI gate |
> | Betting | CLV engine (implied/de-vig/edge/EV/CLV) + prop-odds ingestion |
> | Validation | backtester (ECE/Brier/log-loss + ablation), chi-squared replay, sniff, invalid-state harness |
> | Tests | **1505 unit+regression passing / 1 skipped / 0 failed** (+9 slow); **perf 5 passed / 0 skipped** |
> | DuckDB schema | **v7** (migrations 0001→0007); Postgres Alembic head 0013 |
>
> **Next: Phase 5 — Backend API & Simulation Runner.** Entry plan: `docs/HANDOFF_PHASE5.md`
> (start with a 9-agent program audit to file the Phase 5 tickets; next free ID **SIM-350**).
> Standing follow-ups: SIM-315 (move off OneDrive — biggest infra risk, still Open), prop-TB
> 2B/3B tracking, the dead `GameSpec._hit_rate` knob.
>
> ---
>
> # 🔭 Phase-4-Close Program Audit — 28 Phase 5 tickets filed (2026-07-15)
>
> The 9 agent scopes (3 clusters + PM) reviewed the project for **Phase 5 (Backend API &
> Simulation Runner)**. 28 tickets consolidated (**SIM-350 … SIM-377**) + the **SIM-315**
> carryover into `docs/audit/2026-07-15-phase5-prioritized-tickets.md` (per-agent findings in
> `docs/audit/2026-07-15-phase4-close-program-audit.md`). Four ⚠ defects found
> (`docker-compose` mounts the empty `./simulator`; `api/` missing from the coverage gate;
> `GameSpec._hit_rate` TypeError; no spread/run-line edge).
>
> **Headline:** the `api/` layer is greenfield — all 6 endpoints + the JSON serialization
> contract + auth are unbuilt; `BatchRunner` has no production DB-backed factory yet.
>
> **Tier P0 gates:** SIM-350 (serialization contract), SIM-351 (auth baseline), SIM-352 (real
> DB-backed `machine_factory`), SIM-353 (lineup/sub resolver — the SIM-338 gap), SIM-354 (mount
> the existing routers/pipeline into `api/main.py`).
> **P1:** SIM-355–361 (endpoints + snapshot persistence + Redis TTL + pool lifecycle + calibration serving).
> **P2:** SIM-362–366 (loop-output gaps: per-inning R/H/E, fielders, W/L/S, richer boxscore); SIM-367–370 (betting surface: spread edge, line-movement, bet-signal, real odds).
> **P2/P3:** SIM-371–374 (E2E/WS tests, SLA perf gate, nginx, monitoring); SIM-375–377 (⚠ hygiene) + **SIM-315** (OneDrive).
> Critical path: SIM-350 → SIM-352/SIM-353 → SIM-355 → SIM-356 → SIM-357/SIM-358.
>
> Phase 5 entry plan: `docs/HANDOFF_PHASE5.md`. **Next free ID after this audit: SIM-378.**
>
> ---
>
> # 🏁 Phase 2 — CLOSED 2026-05-20
>
> **All 11 similarity engines built. Both performance index gates passing
> against real 2024 staging data. Test suite green (767 passed / 22 skipped).
> Project is now in Phase 3.**
>
> | Layer | Status |
> |---|---|
> | Similarity engines | 11 / 11 built (pitcher GMM-W₂; batter/fielder/baserunner-advance/baserunner-steal/catcher-v2/pitcher-steal/manager RBF; situation KDTree; pitch-pitch + batted-ball FAISS) |
> | DB schema | 12 Alembic migrations (`0001 → 0012`); **3 DuckDB migrations on disk (`0001 → 0003`)** — SIM-051 `0003_sim051_pull_relative_spray_angle.sql` rebuilt 2026-05-20 (§7 reconciliation) |
> | Performance gates | SIM-085 `idx_pitches_situation` PASS; SIM-089 `idx_pitches_pitcher_season_clean` PASS (live 2024 staging) |
> | Test infrastructure | **927 unit+regression passing / 1 skipped / 0 failed** after Phase 3 Completion (2026-06-10); 3 perf benches passing. (870 after 2026-06-03; 834 after 2026-05-27; re-baselined from the prior 767 figure.) |
> | DuckDB schema version | **5** — migration 0004 (recency_weight + pool_build_metadata, SIM-076) and 0005 (index prune, SIM-115) added this phase. |
> | Phase 3 architecture spec | SIM-300 doc (`docs/architecture/2026-05-20-play-pool.md`) **reconstructed 2026-05-20** — Phase 3 implementation underway: SIM-301 (cache serializer) + SIM-302 (sampler) shipped (HANDOFF_PHASE3.md §7) |
> | Audit | 9-agent audit was conducted but the two output docs (`docs/audit/2026-05-21-*`) are **missing on disk** — same OneDrive truncation pattern. 53-ticket follow-up summary captured in HANDOFF_PHASE3.md until docs are rewritten |
>
> **Phase 3 entry point:** Tier-P0 tickets from the audit drive the next two
> sprints — SIM-118 (perf benchmark harness), SIM-202 (centralized run-value
> constants), SIM-280/SIM-281 (RAM budget + ProcessPool architecture decision),
> SIM-301 (play-pool nightly cache), SIM-302 (sampler API), SIM-303 (Phase 4
> wiring), SIM-323 (manager decision logic spec), SIM-220 (backtesting
> framework).  Full picture in
> `docs/audit/2026-05-21-prioritized-tickets.md` and
> `docs/HANDOFF_PHASE3.md`.

> # 🔭 Phase-3-Close Program Audit — 41 Phase 4 tickets filed (2026-06-10)
>
> All 9 agents reviewed the project. 41 tickets consolidated (SIM-220 + SIM-310–349)
> into `docs/audit/2026-06-10-phase4-prioritized-tickets.md` (per-agent findings in
> `docs/audit/2026-06-10-phase3-close-program-audit.md`). Full detail per ticket lives
> in `backlog.xlsx` (Full Backlog) and the audit docs. Six **live bugs** were found
> (⚠) to fix as touched. Phase 4 entry plan is in `docs/HANDOFF_PHASE4.md`.
>
> **Tier P0 — gates before loop coding:**
>
> | ID | Title | Owner |
> |---|---|---|
> | SIM-310 | Canonical Phase 4 sim-loop spec (8 steps, fingerprints, terminal logic) | Backend + BA |
> | SIM-311 | GameState + PlayResult dataclass contract | Backend + DE |
> | SIM-312 ⚠ | Fix RUN_VALUES↔Statcast `events` mismatch + run-resolution (result_* + RE24) | BA + Backend |
> | SIM-313 ⚠ | Wire `recency_weight` into the sampler distance-weight | ML + Backend |
> | SIM-314 ⚠ | Resolve SIM-200/201 ID collision | PM |
> | SIM-315 ⚠ | Move repo off OneDrive / file-integrity guard | QA/DevOps |
>
> **P0 status (Sprint 2026-06-17 — CLOSED 2026-05-22):** SIM-310 / 311 / 312⚠ / 313⚠ / 314⚠ ✅ Closed · SIM-322⚠ / 337⚠ ✅ Closed (pulled forward) · SIM-315⚠ documented & deferred (Open). Suite 927 → 1001.
>
> **Tier P1 (loop + validation):** SIM-316–326, SIM-321, SIM-322⚠, SIM-323, SIM-220.
> **Tier P2 (outputs/perf/betting):** SIM-327–340 (incl. SIM-336⚠, SIM-337⚠).
> **Tier P3 (hygiene/tech-debt):** SIM-341–349 (incl. SIM-345⚠, SIM-346⚠).
> Critical path: SIM-310→311→316→317→{318,319}→320→{220,327,332}.
>
> **P1 loop status (Sprint 2026-06-24 — CLOSED 2026-05-23):** SIM-316/317/318/319/320 (loop) + SIM-321 (fusion) + SIM-324/326 (validation harnesses) ✅ Closed — `simulate_game()` now produces full games. Remaining P1: SIM-220 backtester, SIM-323 manager logic, SIM-325 chi-squared replay.
>
> **P1/P2 status (Sprint 2026-07-01 — CLOSED 2026-05-23):** SIM-220 + SIM-325 (validation spine) ✅ Closed · SIM-327/328/330/331/332 (output contracts + batch runner) ✅ Closed. Remaining P1: SIM-323 manager logic. Suite 1144 → 1271.
>
> **P2/P3 status (Sprint 2026-07-08 — CLOSED 2026-05-23):** SIM-329/339/340 (betting chain) + SIM-333 (shared-memory) ✅ Closed · audit bugs SIM-336⚠/345⚠/346⚠ ✅ Fixed — **all six ⚠ live bugs now closed**. Schema v6→v7. Suite 1271 → 1380. Remaining P1: SIM-323 manager logic.
>
> **P3/close status (Sprint 2026-07-15 — CLOSED 2026-05-24):** SIM-323 manager + SIM-349 situational ✅ · SIM-334 columnarize + SIM-335 perf-benches + SIM-347 stress + SIM-348 live-tests ✅ · SIM-341/342/343/344 hygiene ✅. **PHASE 4 COMPLETE.** Suite 1380 → 1505; perf 3/2 → 5/0. (SIM-342 re-categorization: SIM-107 → done via SIM-348; SIM-120 → unblocked by SIM-320; SIM-127/128/129 → Phase 6 frontend.)
>
> ---
>
> # 📋 SIM-382 — Phantom Ticket Backfill (2026-05-25)
>
> *Owner: Product Manager (Agent 1). Executed as part of Phase-6 Sprint 1.*
>
> The Phase-5-close program audit (QA-confirmed) found that the pre-existing Phase-6 frontend
> tickets **SIM-127–131** all cite parent tickets **SIM-108, SIM-109, SIM-112, SIM-122–126** that
> never appeared anywhere in the backlog.  SIM-382 backfills them as retrospective stubs, records
> the intent each would have captured, and re-maps each child ticket's dependency to the real
> Phase-6 ticket that supersedes it.
>
> ### Phantom parent stubs (SIM-108/109/112/122–126)
>
> These were intended as backend-contract / spec tickets filed during Phase 3 planning.  They were
> referenced but never formally created; the IDs sat in the 099–130 "Backend / live pipeline" band.
> All intent is now covered by completed Phase-4/5 tickets or open Phase-6 tickets.
>
> | ID (phantom) | Intended title | Superseded by |
> |---|---|---|
> | SIM-108 | Frontend-facing game-simulation API contract spec | SIM-355 (GET /simulate) + SIM-358 (with_override) ✅ Phase 5 |
> | SIM-109 | Team/venue metadata API (names, abbreviations, venue) | **SIM-383** (games enrichment — Phase 6 Sprint 1) |
> | SIM-112 | Live in-progress game-state read path | **SIM-386** (live state read path — Phase 6 Sprint 2) |
> | SIM-122 | WebSocket event schema | **SIM-385** ✅ Phase 6 Sprint 1 |
> | SIM-123 | Player prop distributions + boxscore API | SIM-366 (boxscore card) ✅ Phase 5 + **SIM-390** (prop edges) |
> | SIM-124 | Frontend scaffold + build-tooling specification | **SIM-378/379** ✅ Phase 6 Sprint 1 |
> | SIM-125 | Managerial override API (multi-substitution) | **SIM-388** (multi-sub override — Phase 6 Sprint 2) |
> | SIM-126 | Design-system + component library specification | **SIM-380** ✅ Phase 6 Sprint 1 |
>
> ### Dependent child tickets (SIM-127–131) — re-mapped dependencies
>
> | ID | Title | Original phantom dep | Re-mapped to |
> |---|---|---|---|
> | SIM-127 | Game card — staleness/CI indicator | SIM-108/122 | **SIM-384** (aggregate card) + **SIM-385** ✅ |
> | SIM-128 | Managerial override — staged queue + undo UI | SIM-125 | **SIM-398** (override v2; depends on **SIM-388**) |
> | SIM-129 | Per-player prop/boxscore UI | SIM-123 | **SIM-394** (per-player boxscore; depends on **SIM-390**) |
> | SIM-130 | CLV/betting surface UI | SIM-109/123 | **SIM-395/396** (betting card + CLV chart) |
> | SIM-131 | Frontend CI/CD pipeline | SIM-124 | **SIM-379/401** ✅ CI done; CD = SIM-401 |
>
> SIM-382 **CLOSED 2026-05-25** — no code change; backlog and audit docs updated.
>
> ---
>
> # ✅ Sprint 2026-07-08 — Phase 4 Betting Chain + Bug Cleanup — CLOSED 2026-05-23
>
> All 7 tickets shipped and accepted after cross-validation
> (1380 unit+regression passing / 1 skipped / 0 failed; +6 slow; 3 perf benches). Schema v6→v7.
> Betting chain is end-to-end; the last two ⚠ audit bugs cleared (all six now fixed).
> Full record: `docs/SPRINT_2026-07-08_phase4_betting_bugs.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-329 | Backend + ML + Betting | ✅ Prop PMFs (`simulation/prop_distributions.py`) — full PMF + over/under per prop |
> | SIM-339 | Betting + ML | ✅ CLV engine (`betting/clv_engine.py`) — implied/de-vig/edge/EV/CLV |
> | SIM-340 | Data + Betting | ✅ Prop-odds ingestion wired + `mark_closing_prop_lines` + multi-book/sharp/opening; Alembic 0013 |
> | SIM-336 ⚠ | BA + Data | ✅ Park-factor UNPIVOT/ordering fix + real L/R splits + neutralization policy |
> | SIM-345 ⚠ | Data | ✅ Data-layer fixes (watermark `>=`, consistent recency_ref_season, NOT NULL parity, stand contract); DuckDB 0007, schema v7 |
> | SIM-346 ⚠ | ML | ✅ Calibration — no-arsenal redistribution, one linear arsenal scale, CalibrationReport wired, drift test |
> | SIM-333 | Perf | ✅ Shared-memory zero-copy attach (≤2 GB at W workers); per-worker fallback |
>
> **All six ⚠ audit live bugs now fixed** (SIM-312/313/322/337/336/346).
> **Next: Sprint 5** — SIM-323 manager logic + SIM-349 situational; SIM-334/335 perf; SIM-347/348 tests; P3 hygiene SIM-341–344. `backlog.xlsx` needs regen.
>
> ---
>
> # ✅ Sprint 2026-07-01 — Phase 4 Validation Spine + Output Contracts — CLOSED 2026-05-23
>
> All 7 tickets shipped and accepted after cross-validation
> (1271 unit+regression passing / 1 skipped / 0 failed; +5 slow; 3 perf benches).
> The output-contract layer + validation spine now exist end-to-end.
> Full record: `docs/SPRINT_2026-07-01_phase4_validation_outputs.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-327 | Backend + UX | ✅ `GameSimSummary` aggregation (`simulation/results.py`) — win%/mean/median/raw per-iter arrays/CIs |
> | SIM-328 | Backend + BA | ✅ Per-player `BoxScore` accumulators (AB/H/HR/RBI; IP/K/BB/ER) in the PA loop |
> | SIM-332 | Backend + Perf | ✅ ProcessPool 100-iter batch runner + Redis-TTL-with-fallback; SIM-333 seam |
> | SIM-330 | Backend + ML | ✅ Calibrated win-probability (Beta smoothing + CI + calibration-map seam) |
> | SIM-331 | Backend + UX | ✅ Field/PBP snapshot contracts (FieldSnapshot/PlayByPlay/StateAtPitch/OverrideDelta) |
> | SIM-220 | ML + Betting | ✅ Backtester — ECE/Brier/log-loss + reliability + ablation vs league-average |
> | SIM-325 | QA + BA | ✅ Chi-squared historical-replay GOF (p≈0.36; negative control rejected) |
>
> **Next: Sprint 4** — SIM-329 prop PMFs + SIM-339/340 CLV/odds; SIM-333 shared-memory;
> SIM-323 manager logic; audit bugs SIM-336/SIM-346. `backlog.xlsx` needs regen.
>
> ---
>
> # ✅ Sprint 2026-06-24 — Phase 4 Loop Build — CLOSED 2026-05-23
>
> All 8 tickets shipped and accepted after independent QA cross-validation
> (1144 unit+regression passing / 1 skipped / 0 failed; +2 slow; 3 perf benches).
> The SIM-303 scaffold is now a full-game simulator; critical path
> SIM-310→311→316→317→{318,319}→320 COMPLETE.
> Full record: `docs/SPRINT_2026-06-24_phase4_loop_build.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-316 | Backend | ✅ GameState count/out/inning state machine (`sim_loop.py`) |
> | SIM-321 | ML + Backend | ✅ Cross-engine score-fusion module + design doc (distance→weight stays in sampler) |
> | SIM-317 | ML + Backend | ✅ Real 10-dim/3-dim fingerprint derivation, wired into the loop |
> | SIM-318 | Backend + BA | ✅ Outcome step 4 + SIM-056 count-conditional foul re-weight |
> | SIM-319 | Backend + ML | ✅ Fielding + baserunning + steals + dropped-3rd-strike; all run deltas via `resolve_runs` |
> | SIM-320 | Backend | ✅ `simulate_game()` — regulation/walk-off/extras+ghost/seeding; returns `GameSimResult` (unblocks SIM-120) |
> | SIM-326 | QA + Backend | ✅ Invalid-state harness — 1,000 games, zero invalid states |
> | SIM-324 | BA + QA | ✅ Sniff suite — run env ≈4.4 R/G, P/PA ≈3.7, platoon emerges, RE24 monotonic |
>
> **Next: Sprint 3** — SIM-220 backtester + SIM-325 chi-squared replay (validation spine),
> SIM-323 manager logic, P2 output contracts (SIM-327/328/330) + perf (SIM-332/333).
> `backlog.xlsx` needs regen from this file.
>
> ---
>
> # ✅ Sprint 2026-06-17 — Phase 4 P0 Gates — CLOSED 2026-05-22
>
> All 8 tickets shipped and accepted after independent QA cross-validation
> (996 unit+regression passing / 1 skipped / 0 failed; +60 subtests; 3 perf benches;
> 1001 after restoring the corrupted `test_data_engineer_sim162.py`). Opens Phase 4.
> Full record: `docs/SPRINT_2026-06-17_phase4_p0_gates.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-310 | Backend + BA | ✅ Canonical Phase 4 sim-loop spec (one 8-step loop) |
> | SIM-311 | Backend + DE | ✅ `GameState` + `PlayResult` contract (`simulation/game_state.py`) |
> | SIM-312 ⚠ | BA + Backend | ✅ RUN_VALUES↔events fix + `run_resolution.py` (RE24 + linear fallback) |
> | SIM-313 ⚠ | ML + Backend | ✅ `recency_weight` wired into `PlayPoolSampler` |
> | SIM-322 ⚠ | ML Eng | ✅ GMM covariance double-standardization fixed (engine-side) |
> | SIM-337 ⚠ | DE + Perf | ✅ sim-pool indexes reconciled to SIM-111 contract (migration 0006, schema v6) |
> | SIM-314 ⚠ | PM | ✅ SIM-200/201 ID collision resolved (manager logic = SIM-323) |
> | SIM-315 ⚠ | QA/DevOps | 📄 Remediation plan documented; deferred — ticket stays **Open** |
>
> **4 of 6 audit live bugs fixed** (SIM-312/313/322/337); remaining SIM-336, SIM-346.
> **Next: Phase 4 loop build** — SIM-316→317→{318,319}→320 (`simulate_game()`) + the
> SIM-220 validation spine. `backlog.xlsx` needs regen from this file.
>
> ---
>
> # 🏁 Phase 3 — Play Pool Architecture — COMPLETE (2026-06-10)
>
> All play-pool tickets shipped and accepted after independent QA
> (927 unit+regression passing / 1 skipped / 0 failed; 3 perf benches).
> Record: `docs/SPRINT_2026-06-10_phase3_completion.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-048 | ML Eng | ✅ SimilarityEngineRegistry (`similarity/registry.py`) |
> | SIM-076 | Data Eng + ML Eng | ✅ recency_weight + pool_build_metadata + migration 0004 + walk-forward harness |
> | SIM-095 | Data Eng | ✅ Incremental pool rebuild |
> | SIM-111 | Backend + Data Eng | ✅ Play-pool query column contracts |
> | SIM-115 | Data Eng + Perf Eng | ✅ Prune sim-pool indexes (migration 0005) |
> | SIM-056 | Baseball Analyst | ✅ Count-stratified foul-ball weighting design |
>
> Phase 3 flagship (prior sprints): SIM-300 spec, SIM-301 cache, SIM-302 sampler, SIM-303 sim-loop wiring.
> **Still open (NOT play-pool):** SIM-127/128/129 (frontend, Phase 6), SIM-107 (live-pipeline tests), SIM-120 (needs Phase 4 simulate_game).
> **Next: Phase 4** — flesh out the SIM-303 scaffold into the full simulation loop; SIM-220 backtesting; SIM-323 manager logic.
>
> ---
>
> # ✅ Sprint 2026-06-03 — Phase 4 Readiness — CLOSED 2026-05-21
>
> All 7 tickets shipped and accepted after independent QA cross-validation
> (870 unit+regression passing / 1 skipped / 0 failed; 3 perf benches passing).
> Full record in `docs/SPRINT_2026-06-03_phase4_readiness.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-114 | Perf+ML | ✅ FAISS index design spec (per-tile flat; IVFFlat >50k crossover) |
> | SIM-303 | Backend | ✅ PlayPoolSampler wired into sim-loop scaffold (Phase 3 complete) |
> | SIM-119 | Perf+BE | ✅ Per-step time budget for the 8-step loop |
> | SIM-113 | Perf+DE | ✅ GMM batch: dynamic workers + chunked IPC + bulk writes |
> | SIM-075 | ML+Perf | ✅ Arsenal W2 cache vectorized (~2.9×, identical results) |
> | SIM-074 | Data Eng | ✅ barrel_rate full Statcast sliding-scale definition |
> | SIM-090 | Data Eng | ✅ ETL psycopg2 connection pool |
>
> **Next:** SIM-220 (backtesting), SIM-323 (manager logic), Phase-4 loop steps; perf follow-ups (share arsenal cache, columnarize situation engine).
>
> ---
>
> # ✅ Sprint 2026-05-27 — Phase 3 Kickoff — CLOSED 2026-05-20
>
> All 11 work items shipped and accepted after independent QA cross-validation
> (833 unit+regression passing / 1 skipped / 0 failed; 3 perf benches passing).
> Full record in `docs/SPRINT_2026-05-27_phase3_kickoff.md` and `CHANGES.md`.
>
> | Ticket | Owner | Status |
> |---|---|---|
> | SIM-300 | PM→BE+ML | ✅ Spec reconstructed (`docs/architecture/2026-05-20-play-pool.md`) |
> | SIM-051 | Data Eng | ✅ DuckDB migration `0003` + 7 tests |
> | SIM-162 | Data Eng | ✅ LeagueAverageProfiles regression (5 tests) |
> | SIM-149 | ML Eng | ✅ Baserunner-steal unit file (9 invariants) |
> | SIM-150 | ML Eng | ✅ Calibration regressions (catcher v2 / FAISS ×2) |
> | SIM-202 | Baseball Analyst | ✅ `simulation/constants.py` RUN_VALUES + DEFENSIVE_RUN_VALUES |
> | SIM-118 | Perf Eng | ✅ Benchmark harness + weekly CI |
> | SIM-301 | Backend | ✅ Play-pool nightly cache serializer |
> | SIM-302 | Backend+ML | ✅ `PlayPoolSampler` four-method API |
> | SIM-280 | Perf Eng | ✅ RAM budget vs 2 GB (measured) |
> | SIM-281 | Perf Eng | ✅ Parallelism ADR (ProcessPoolExecutor + shared_memory) |
>
> **Still open (non-P0 §7):** `docs/audit/2026-05-21-*.md` rebuilds.
> **Next:** SIM-303 (wire sampler into sim loop), SIM-220 (backtesting), SIM-323 (manager logic).
> **Note:** `backlog.xlsx` was locked during the sprint — regenerate from this file to publish the closed state.

> **Audit 2026-05-21:** end-of-Phase-2 program audit conducted by all 9
> agents.  Findings and the prioritized ticket list live in
> `docs/audit/2026-05-21-program-audit.md` and
> `docs/audit/2026-05-21-prioritized-tickets.md`.  53 tickets filed
> (47 new + 6 pre-existing).  Tier-P0 gating tickets must land before
> Phase 4 simulation-loop work begins.

This file is the canonical home for **proposed and in-flight work**. Completed work moves to `CHANGES.md` once shipped.

> **`backlog.xlsx` health note (2026-05-14, resolved):** the previously
> corrupted workbook was rebuilt from `BACKLOG.md` as `backlog_v2.xlsx` on
> 2026-05-14 and renamed to `backlog.xlsx` by the user.  Both sources are
> now in sync.  `BACKLOG.md` remains the authoritative draft surface;
> `backlog.xlsx` is the published artifact that gets regenerated at the
> end of each sprint.

**Companion files:**
- `CHANGES.md` — completed sprints, organized by agent
- `agent_team.md` — definitions of the 9 agents and ownership scopes
- `PRODUCT_GUIDE.md` — onboarding and concept reference for newcomers

**Conventions:**
- Tickets use `SIM-XXX` IDs. New IDs continue sequentially within the appropriate band (see "ID bands" below).
- Each ticket lists **Type / Effort / Phase / Owners / Depends on / Acceptance criteria**.
- A ticket is shippable only when every acceptance criterion passes. PM owns the acceptance gate; named agents implement and self-verify.
- Forward-looking placeholder tickets (Phase 4+) are explicitly marked. Their acceptance criteria are drafted now to lock requirements before the surrounding spec is written.

**ID bands (informal):**
- `SIM-040–070` — Phase 2 similarity engines
- `SIM-080–099` — Data engineer infrastructure / migrations
- `SIM-099–130` — Backend / live pipeline bugs and features
- `SIM-130–149` — Odds, CLV, prop markets, CI/CD
- `SIM-200+` — Phase 4 simulation loop design constraints

---

## Sprint 2026-05-06 + 2026-05-07 — Data + Backend Stabilisation (CLOSED 2026-05-07)

**Sprint disposition:** ✅ all 16 tickets accepted by PM on 2026-05-07.
Full delivery details in `CHANGES.md` under the corresponding sprint headers.

| Ticket | Type | Owner | Status |
|---|---|---|---|
| SIM-085 | Bug | Data Engineer | ✅ Shipped — composite situation index on `raw.pitches` |
| SIM-086 | Bug | Data Engineer | ✅ Shipped — `raw.games.venue_id` nullable + `venue_backfill_job.py` |
| SIM-087 | Bug | Data Engineer | ✅ Shipped — release_speed validator/trigger thresholds lowered |
| SIM-088 | Improvement | Data Engineer | ✅ Shipped — dropped `idx_pitches_pitch_type` |
| SIM-089 | Improvement | Data Engineer | ✅ Shipped — composite `(pitcher, season)` partial index |
| SIM-091 | Bug | Data Engineer | ✅ Shipped — `_delete_seasons()` regression guard |
| SIM-092 | Improvement | Data Engineer | ✅ Shipped — `raw.game_odds` deduplication via SHA-256 hash |
| SIM-093 | Gap | Data Engineer | ✅ Shipped — `raw.etl_errors` audit table + ETL wiring |
| SIM-101 | Bug | Backend Developer | ✅ Shipped — per-game GameStateBuilder cache |
| SIM-102 | Bug | Backend Developer | ✅ Shipped — Opener role classification |
| SIM-103 | Bug | Backend Developer | ✅ Shipped — broadcast() set snapshot |
| SIM-104 | Improvement | Backend Developer | ✅ Shipped — /resimulate Redis cooldown |
| SIM-105 | Improvement (P2) | Backend Developer | ✅ Shipped — completed-game upsert skip |
| SIM-106 | Improvement | Backend Developer | ✅ Shipped — async-callable type guard |
| SIM-148 | Bug | ML Engineer + QA/DevOps | ✅ Shipped (with documented deviation) — pitcher_similarity test cleanup |
| SIM-153 | Gap | QA/DevOps + Backend Developer | ✅ Shipped — secrets management baseline |

**PM acceptance verdict:**
- 11 Alembic migrations now in chain (`0001 → 0011`); chain integrity verified.
- 66/66 unit tests passing on the new ticket-specific suites (2 environmental skips for missing scipy in sandbox; CI installs scipy).
- `CHANGES.md` documents every ticket with deltas, rationale, and verification commands.
- `agent_team.md` migration workflow (SIM-084) honoured: every PostgreSQL schema change shipped with an Alembic migration.

**Follow-ups generated by this sprint** (entered as new tickets below):

| New ticket | Source | Effort | Owner |
|---|---|---|---|
| **SIM-157** | SIM-092 carry-forward | S | Data Engineer |
| **SIM-158** | SIM-085 + SIM-089 acceptance gates | S | Performance Engineer |
| **SIM-159** | SIM-132 RNG vig-boundary flake | S | Backend Developer |

(Note: SIM-094/095/096 are PRE-EXISTING open Phase 2 polish tickets; the three sprint-2026-05-13 follow-ups were renumbered to SIM-157/158/159 to avoid the collision.  PM signed-off the renumber 2026-05-08.)

---

## Sprint 2026-05-13 — Phase 2 Closure & Engine Build-out (CLOSED 2026-05-14)

**Sprint disposition:** ✅ all 7 tickets accepted by PM on 2026-05-14.
Full delivery details in `CHANGES.md` under the "Sprint 2026-05-13" header.

| Ticket | Type | Owner | Status |
|---|---|---|---|
| SIM-073 | Gap | Data Engineer | ✅ Shipped — `steal_attempt_rate_against` column on `derived.catcher_season_metrics`; migration 0002; profile computor populates it. |
| SIM-072 | Enhancement | ML Engineer | ✅ Shipped — CatcherSimilarityEngine v2 (5-sub-score split: Framing 45 + Blocking 20 + Execution 12 + Deterrence 8 + Offense 15). |
| SIM-157 | Improvement | Data Engineer | ✅ Shipped — `scripts/backfill_odds_hash.py` + Alembic 0012 promotes partial → full unique index after backfill. |
| SIM-158 | Validation | Performance Engineer | ✅ Shipped (harness) — `scripts/run_index_acceptance.py` + acceptance doc.  Live EXPLAIN ANALYZE run deferred until 2024 staging data is loaded (PM-approved). |
| SIM-159 | Bug | Backend Developer | ✅ Shipped — moneyline vig test bounds widened to absorb American-odds integer rounding; deterministic across 100 runs × 5 game_pks. |
| SIM-041 | Feature | ML Engineer | ✅ Shipped — `PitchPitchSimilarityEngine` (FAISS IndexFlatL2 + HNSW path) over 10-dim pitch fingerprint. |
| SIM-042 | Feature | ML Engineer | ✅ Shipped — `BattedBallSimilarityEngine` (FAISS) over 3-dim launch fingerprint with SIM-051 fall-forward (uses `pull_relative_spray_angle` automatically when shipped). |

**PM acceptance verdict:**
- 12 Alembic migrations now in chain (`0001 → 0012`, with 0012 conditional on the SIM-157 backfill running first).
- 2 DuckDB migrations now in chain (`0001 → 0002`).
- 95/95 unit + regression tests passing across the new and existing engines (10 environmental skips for missing scipy in sandbox — CI installs scipy).
- 11 of 11 similarity engines now built: pitcher (GMM W₂), batter (RBF), fielder (RBF), baserunner extra-base (RBF), baserunner steal (RBF), catcher (RBF v2), pitcher-steal (RBF), manager (RBF), situation (KDTree), pitch-to-pitch (FAISS), batted-ball (FAISS). **Phase 2 milestone reached.**

**Follow-ups generated by this sprint** (entered as new tickets below):

| New ticket | Source | Effort | Owner |
|---|---|---|---|
| **SIM-160** | SIM-042 / SIM-051 dependency | S | Data Engineer |
| **SIM-161** | SIM-158 live EXPLAIN ANALYZE execution | S | Performance Engineer |
| **SIM-162** | `pipeline/batch/player_profile_computor.py` pre-existing truncation in `LeagueAverageProfiles.compute()` | S | Data Engineer |

---

## Sprint 2026-05-20 — Phase 2 hardening & Phase 3 kickoff (CLOSED 2026-05-21)

**Sprint disposition:** ✅ All 7 tickets shipped. SIM-161 was initially deferred for staging data, but the live EXPLAIN ANALYZE run completed out-of-sprint on 2026-05-20 against developer-local Postgres — both gates pass (SIM-089: 6.77 ms / 50 ms; SIM-085: passing after `_build_situation_query` was rewritten in SIM-163 to emit literal `IS NULL` instead of parameterized `IS NOT DISTINCT FROM`, which had triggered a 12x prepared-statement regression).

| Ticket | Type | Owner | Status |
|---|---|---|---|
| SIM-051 | Improvement | Data Engineer | ✅ Shipped — `pull_relative_spray_angle` column on `sim.outcome_pool`; DuckDB migration 0003; populated at ETL time via stand/bat_hand handedness flip. SIM-042's loader picks it up automatically. |
| SIM-160 | Gap | Data Engineer | ✅ Shipped — `scripts/check_bat_side_coverage.py` audit script + acceptance doc; gate threshold 1 % NULL per season. |
| SIM-162 | Bug | Data Engineer | ✅ Shipped — restored `player_profile_computor.py` truncated tail; module parses cleanly; chains `LeagueAverageProfiles.compute()` from the entry point. |
| SIM-149 | Gap | QA / DevOps | ✅ Shipped — `tests/unit/test_baserunner_steal_engine.py` covers all 9 invariants. Phase 2 closure complete: every engine has a unit test file. |
| SIM-150 | Gap | QA / DevOps | ✅ Shipped — `tests/unit/test_ml_engines_sim150.py` covers catcher v2 Realmuto-archetype top-10 sanity, pitch-to-pitch recency-boost effect, batted-ball outcome monotonicity. |
| SIM-161 | Validation | Performance Engineer | ✅ Shipped 2026-05-20 — both gates pass against live 2024 staging. SIM-089 = 6.77 ms / 50 ms. SIM-085 passing after SIM-163 fix to `_build_situation_query` (replaced parameterized `IS NOT DISTINCT FROM` with literal `IS NULL` for None-valued bases — prepared-statement quirk caused 12x regression on the initial run). Report committed to `docs/perf/2026-05-13-index-acceptance.md`. |
| SIM-300 | Spec | Backend Developer + ML Engineer | ✅ Shipped — `docs/architecture/2026-05-20-play-pool.md` defines the Phase 3 sampler architecture: pre-filter contract, sub-index materialization, recency lifecycle, sampler query API, performance budget. Implementation tickets drafted as SIM-301+. |

**PM acceptance verdict (2026-05-21):**
- 3 DuckDB migrations now in chain (`0001 → 0003`); chain integrity verified.
- 12 Alembic migrations in chain unchanged from sprint 2026-05-13.
- 120/120 unit + regression tests passing (10 environmental skips for scipy in sandbox).
- Phase 2 hardening complete — every engine has a unit test file; calibration extensions for the v2 catcher + both FAISS engines locked in.
- Phase 3 spec accepted as the first Phase 3 deliverable; implementation tickets will be drafted at sprint 2026-05-27 kickoff.

**Follow-ups generated by this sprint:**

| New ticket | Source | Effort | Owner |
|---|---|---|---|
| **SIM-301** | SIM-300 spec — play-pool cache | M | Backend Developer |
| **SIM-302** | SIM-300 spec — sampler API | M | Backend Developer + ML Engineer |
| **SIM-303** | SIM-300 spec — Phase 4 wiring | M | Backend Developer |

---

## Closed-sprint reference — Sprint 2026-05-20 (original proposal)

**Sprint goal:** finish the Phase 2 hardening tasks deferred from 2026-05-13 (regression test files for the two new FAISS engines, SIM-051 pull-relative spray angle, live SIM-158 run), then begin Phase 3 play-pool architecture work now that all 11 similarity engines are built.

**Total scope:** 7 tickets · 0 of L · 3 of M · 4 of S.
Estimated team-effort: ~8 dev-days against ~6 calendar-days. Capacity reasonable.

### Sequence + dependencies

```
SIM-051 (DE, S) ─── pull_relative_spray_angle column
                  └─ unblocks SIM-042 calibration (regression-only impact, no engine code change)

SIM-160 (DE, S) ─── ensure raw.pitches.bat_side present + populated; required by SIM-051

SIM-161 (Perf, S) ── live EXPLAIN ANALYZE run for SIM-085 + SIM-089 once 2024 in staging

SIM-149 (QA/DevOps, S) ── unit test file for baserunner_steal engine (carried from 2026-05-13)
SIM-150 (QA/DevOps, M) ── calibration test extensions for catcher engine v2 + new FAISS engines

SIM-162 (DE, S)   ─── restore truncated LeagueAverageProfiles.compute() in player_profile_computor.py

SIM-300 (BE+ML, M) ── Phase 3 play-pool architecture spec (kickoff)
```

### Sprint commit list

| # | Ticket | Type | Effort | Owner | Why now |
|---|---|---|---|---|---|
| 1 | SIM-051 | Gap | S | Data Engineer | Adds `pull_relative_spray_angle` to `sim.outcome_pool`.  SIM-042's loader is already SIM-051-aware — just needs the column to ship. |
| 2 | SIM-160 | Gap | S | Data Engineer | Verifies `raw.pitches.bat_side` exists + is populated; SIM-051 depends on it. |
| 3 | SIM-162 | Bug | S | Data Engineer | Fix the pre-existing truncation in `LeagueAverageProfiles.compute()` (raised during SIM-073 verification — file cuts off at line 3755 mid-f-string). |
| 4 | SIM-149 | Gap | S | QA / DevOps | Baserunner-steal engine unit test file — only engine without one. |
| 5 | SIM-150 | Gap | M | QA / DevOps | Calibration test extensions for catcher v2 (BA Realmuto top-10 sanity), pitch-to-pitch (FAISS recency boost), batted-ball (HR distribution per launch-window). |
| 6 | SIM-161 | Validation | S | Performance Engineer | Live EXPLAIN ANALYZE run via `scripts/run_index_acceptance.py` against staging once 2024 is loaded.  Pastes results into `docs/perf/2026-05-13-index-acceptance.md`. |
| 7 | SIM-300 | Spec | M | Backend Developer (lead) · ML Engineer | Phase 3 play-pool architecture spec.  All 11 engines now built; Phase 3 can start. |

### Risks flagged for sprint kickoff

1. **SIM-051 + SIM-160 are joined at the hip.** If `bat_side` isn't populated on every loaded season, SIM-051's pull-relative computation is partial-NULL.  PM proposes shipping both together; if `bat_side` is already populated everywhere (check at standup day 1) then SIM-160 becomes a no-op `git grep` confirmation ticket.
2. **SIM-161 staging readiness.** Needs the 2024 season fully loaded into staging Postgres.  If staging is still empty at sprint kickoff, defer to 2026-05-27 and file as ongoing-blocked.
3. **SIM-300 spec quality.** Phase 3 is the first "no similarity engine" sprint in the project.  Backend Dev should pull Baseball Analyst into the spec review — play-pool sampling decisions are at the boundary of ML + simulation.

### Out of scope (explicitly deferred)

- Phase 4+ simulation loop placeholders (SIM-200, SIM-201) — held until Phase 4 spec drafting begins.
- Re-implementing the live ingestion pipeline against Python 3.10 — pyproject.toml fixes Python 3.11+ as the project floor; the sandbox shim is a debugger convenience, not a backlog item.
- Frontend UX work — re-enters scope at Phase 5/6 boundary.

### Closed-sprint references

The **previous** sprint 2026-05-13 — Phase 2 Closure & Engine Build-out — was originally drafted with the following sprint goal: "close out the Phase 2 similarity engine suite (only 2 of 11 engines remain) and ship the highest-priority ML-engineer plumbing tickets that unblock Phase 3 play-pool architecture work."  All 7 tickets shipped on schedule; see the disposition table above and the detailed entries in `CHANGES.md`.

---

## Standing tickets (not in current sprint, kept here for visibility)

These were drafted in earlier sprints, accepted by PM as future-work, and are
documented at full fidelity below so they survive any backlog.xlsx loss.

### SIM-072 — CatcherSimilarityEngine v2: Split Throwing into Execution + Deterrence

**Type:** Enhancement | **Effort:** M | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-14 (sprint 2026-05-13)
**Owners:** ML Engineer (lead) · Baseball Analyst (validation) · QA/DevOps (regression fixtures)
**Depends on:** SIM-073 (data dependency)
**Supersedes:** Composite weight scheme established in SIM-067

#### Acceptance criteria

1. New `deterrence_score` field added to `CatcherSimilarityResult` (8% weight)
2. Existing `throwing_score` reduced to 12% weight; retains `pop_time_avg`, `cs_rate`, `exchange_time_avg`, `arm_strength_mph`
3. New `deterrence_score` uses `steal_attempt_rate_against` as its sole feature (single-feature sub-score is acceptable for v1)
4. Composite weights sum to 1.0: Framing 45 + Blocking 20 + Execution 12 + Deterrence 8 + Offense 15 = **100**
5. `EB_N_PRIOR=15` retained for both throwing-derived sub-scores
6. Regression fixtures regenerated: `python tests/regression/generate_fixtures.py --force` and committed
7. `TestWeightConstants` in `tests/regression/test_engine_regression.py` updated to assert new 5-sub-score weight scheme
8. New unit test: a synthetic high-deterrence/low-execution catcher and a low-deterrence/high-execution catcher must score `< 0.40` against each other
9. Sanity check (BA sign-off): top-10 comps for J.T. Realmuto's profile dominated by elite-arm catchers

### SIM-073 — Add `steal_attempt_rate_against` to `derived.catcher_season_metrics`

**Type:** Gap | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-14 (sprint 2026-05-13)
**Owners:** Data Engineer (lead) · Baseball Analyst (formula validation)
**Blocks:** SIM-072

#### Acceptance criteria

1. New column `steal_attempt_rate_against FLOAT` added to `derived.catcher_season_metrics`
2. Numbered DuckDB migration in `db/migrations/duckdb/` (e.g. `0002_catcher_attempt_rate_against.sql`)
3. `db/schemas/duckdb_schema_version.txt` incremented
4. `db/schemas/02_duckdb_schema.sql` updated to reflect the new column (canonical schema source)
5. `pipeline/batch/player_profile_computor.py::_compute_catcher_throwing()` (or equivalent) updated to populate the column
6. **Formula** (BA-approved): `(SB + CS) / (runner_on_1B_opportunities + runner_on_2B_opportunities)`, opportunities counted at PA level (not pitch level), denominator excludes PAs where the runner was forced to advance
7. **Min-sample guard:** column NULL if denominator < 100 PA opportunities
8. Backfill all loaded seasons (2022, 2023, 2024) after migration applies
9. **Sanity check:** the bottom 10 catchers by `steal_attempt_rate_against` should include known elite-arm catchers (Realmuto, Stephenson, Heim-tier)

### SIM-157 — Backfill legacy `odds_hash` + dedup pass

**Type:** Improvement | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-14 (sprint 2026-05-13)
**Owners:** Data Engineer
**Depends on:** SIM-092 (column + index already shipped)

#### Problem

SIM-092 added the `odds_hash` column and partial unique index, but only enforces deduplication going forward. Pre-SIM-092 rows have `NULL odds_hash` and may contain duplicates. CLV queries that join against `raw.game_odds` over historical windows therefore see inflated row counts and slower scans.

#### Acceptance criteria

1. One-shot script `scripts/backfill_odds_hash.py` that: (a) computes `odds_hash` for every NULL-hash row using `LiveIngestionPipeline._odds_hash()`, (b) writes back in batches of 10k, (c) reports duplicate-detection stats.
2. After backfill, a follow-up DELETE keeps only the *earliest* row per `(game_pk, source, odds_hash)` group.
3. Once the table is clean, promote the partial unique index to a full unique index in a new Alembic migration `0012`.
4. Validation: `SELECT COUNT(*) FROM raw.game_odds WHERE odds_hash IS NULL` returns 0.
5. Validation: `SELECT game_pk, source, odds_hash, COUNT(*) FROM raw.game_odds GROUP BY 1,2,3 HAVING COUNT(*) > 1` returns no rows.
6. PR description includes row-counts before/after so storage win is measurable.

### SIM-158 — Run EXPLAIN ANALYZE acceptance gates for SIM-085 + SIM-089

**Type:** Validation | **Effort:** S | **Phase:** 2 | **Status:** ✅ Harness shipped 2026-05-14 (sprint 2026-05-13) — live run deferred to SIM-161 once 2024 staging data is loaded
**Owners:** Performance Engineer (lead) · Data Engineer (data prep)

#### Problem

SIM-085 (composite situation index) and SIM-089 (`(pitcher, season)` partial index) were merged with their acceptance gates *expressed* but not *executed* — the sandbox lacked a populated DB. Once a 2024 staging DB exists, these gates must be run and the results recorded so we can confirm the index choices were correct.

#### Acceptance criteria

1. SIM-085: `EXPLAIN (ANALYZE, BUFFERS)` on a representative situation lookup (count + outs + baserunner state) reports `Index Scan using idx_pitches_situation`, not `Seq Scan on pitches`. Single-query latency < 30 ms on a populated season.
2. SIM-089: `EXPLAIN (ANALYZE, BUFFERS)` on `_compute_pitcher_profiles()`'s primary fetch reports `Index Scan using idx_pitches_pitcher_season_clean`. 3,000-pitch fetch < 50 ms.
3. Results captured in a Markdown report committed under `docs/perf/2026-05-13-index-acceptance.md` and linked from the SIM-085 / SIM-089 entries in CHANGES.md.
4. If either index loses to a Seq Scan, file a follow-up ticket immediately and revert the index claim from CHANGES.md.

### SIM-159 — Tighten SIM-132 vig RNG range so the moneyline test is no longer flaky

**Type:** Bug | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-14 (sprint 2026-05-13)
**Owners:** Backend Developer

#### Problem

`MockOddsAPI.get_odds()` samples vig from `rng.uniform(0.06, 0.10)`, producing an overround of `1 + vig/2 ∈ [1.030, 1.050]`. The regression test asserts strict `> 1.03`, which fails at the lower edge for game_pk=12345 (RNG produces 1.0286 due to floating-point on the inflation path). The flake masks any *real* zero-vig regression.

#### Acceptance criteria

1. Either tighten the RNG floor to `rng.uniform(0.07, 0.10)` so the strict `> 1.03` always holds, OR weaken the test assertion to `>= 1.03 - 1e-9` and add an upper bound `< 1.05 + 1e-9`. PM prefers the test-side change to keep the SIM-132 mock consistent with real sharp-book data.
2. The `[12345]` parametrized case must pass deterministically across 100 consecutive runs.
3. Document the calibration choice in the test file.

### SIM-160 — Verify `raw.pitches.bat_side` populated for all loaded seasons

**Type:** Gap | **Effort:** S | **Phase:** 2 | **Status:** ✅ Shipped 2026-05-21 (sprint 2026-05-20)
**Owners:** Data Engineer
**Blocks:** SIM-051 (pull-relative spray angle needs bat handedness)

#### Problem

SIM-042 ships SIM-051-aware but SIM-051's `pull_relative_spray_angle` formula
requires `bat_side` to be non-NULL on every row.  Before SIM-051 builds,
confirm `bat_side` coverage on `raw.pitches` is ≥ 99 % for every loaded
season (2022, 2023, 2024) and add a backfill or an ETL fix if not.

#### Acceptance criteria

1. `SELECT season, COUNT(*) FILTER (WHERE bat_side IS NULL), COUNT(*) FROM raw.pitches GROUP BY 1` reports ≤ 1 % NULLs per season.
2. If any season exceeds 1 %, file a follow-up data-quality ticket and pull `bat_side` from `chadwick_register` keyed on `batter`.
3. Document the result in `docs/data_quality/2026-05-20-bat-side-coverage.md`.

### SIM-161 — Live EXPLAIN ANALYZE run for SIM-085 + SIM-089

**Type:** Validation | **Effort:** S | **Phase:** 2 | **Status:** ⏳ Deferred (operational) — carry to sprint 2026-05-27. Harness re