#!/usr/bin/env python3
"""Write the file index: every source file, in the order of the code review path.

The index is ``docs/technical/file-index.md``. It lists every tracked source and
config file in the stage of the review path (``docs/technical/code-review-path.md``)
that reads it, with its size, the first sentence of its own docstring or header
comment, and the test files that import it. Data files (test fixtures, script
inputs, logos and fonts) and the documents themselves are not listed.

The stages live in ``STAGES`` below. A file belongs to the stage of the most
specific pattern that matches its path (the pattern with the longest literal
prefix). Within a stage, files follow the order of their patterns, so the key
files of a stage come first.

The page carries a key: a hash of each file's stage, path and first sentence. The
unit test ``tests/unit/test_docs_file_index.py`` recomputes it and fails when a
file is added, removed or moved to another stage, or its first sentence changes.
The line counts and the test lists are not in the key: they refresh whenever the
page is written.

Usage::

    python scripts/docs_file_index.py            # write the page
    python scripts/docs_file_index.py --check    # exit 1 when the page is stale

It needs ``git`` and the standard library only.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / "docs" / "technical" / "file-index.md"
KEY_PREFIX = "<!-- file-index-key: "

#: The review stages, in reading order: (number, title, one-line summary, patterns).
#: The patterns of a stage are in reading order too. The titles must match the
#: stage headings of ``docs/technical/code-review-path.md``.
STAGES: list[tuple[int, str, str, list[str]]] = [
    (
        1,
        "The game's vocabulary",
        "The state of a game, the count machine, the constants and the flags every other file uses.",
        [
            "simulation/game_state.py",
            "simulation/loop/values.py",
            "simulation/constants.py",
            "simulation/flags.py",
            "simulation/__init__.py",
            "simulation/loop/__init__.py",
            "simulation/loop/base.py",
        ],
    ),
    (
        2,
        "One pitch: the loop",
        "The state machine that plays one pitch, one plate appearance and one game.",
        [
            "simulation/sim_loop.py",
            "simulation/loop/terminals.py",
            "simulation/loop/running_game.py",
            "simulation/loop/in_play.py",
            "simulation/loop/manager.py",
            "simulation/loop/credit.py",
            "simulation/loop/boxscore.py",
            "simulation/loop/*",
            "simulation/run_resolution.py",
            "simulation/start_state.py",
        ],
    ),
    (
        3,
        "The draws: the sampler",
        "Every decision is a similarity-weighted draw from a filtered pool of real plays. "
        "The draws happen here.",
        [
            "simulation/full_pool_sampler.py",
            "simulation/sampler/__init__.py",
            "simulation/sampler/base.py",
            "simulation/sampler/kernels.py",
            "simulation/sampler/pitch.py",
            "simulation/sampler/pitch_result.py",
            "simulation/sampler/fielding.py",
            "simulation/sampler/fence.py",
            "simulation/sampler/advancement.py",
            "simulation/sampler/steal.py",
            "simulation/sampler/manager.py",
            "simulation/sampler/*",
            "simulation/filter_cells.py",
            "simulation/synthetic_bundle.py",
        ],
    ),
    (
        4,
        "The sampler's data: the bundle",
        "The nightly build that exports the pools and the score matrices the sampler reads.",
        [
            "pipeline/batch/engine_artifacts.py",
            "pipeline/batch/artifacts/common.py",
            "pipeline/batch/artifacts/pools.py",
            "pipeline/batch/artifacts/pitch_pool.py",
            "pipeline/batch/artifacts/battedball_pool.py",
            "pipeline/batch/artifacts/steal_pool.py",
            "pipeline/batch/artifacts/advancement_pool.py",
            "pipeline/batch/artifacts/manager_pool.py",
            "pipeline/batch/artifacts/pitcher_sim.py",
            "pipeline/batch/artifacts/actor_embeddings.py",
            "pipeline/batch/artifacts/park_geometry.py",
            "pipeline/batch/artifacts/receiving.py",
            "pipeline/batch/artifacts/reference.py",
            "pipeline/batch/artifacts/*",
            "pipeline/batch/bundle_versions.py",
            "pipeline/batch/pool_chain.py",
        ],
    ),
    (
        5,
        "The similarity scores",
        "The eleven engines that score how alike two players or two situations are, "
        "and their calibration.",
        [
            "similarity/engines/rbf_kernel.py",
            "similarity/engines/pitcher_similarity.py",
            "similarity/engines/arsenal_cache.py",
            "similarity/engines/batter_similarity.py",
            "similarity/engines/fielder_similarity.py",
            "similarity/engines/catcher_similarity.py",
            "similarity/engines/baserunner_steal_similarity.py",
            "similarity/engines/pitcher_steal_similarity.py",
            "similarity/engines/baserunner_similarity.py",
            "similarity/engines/manager_similarity.py",
            "similarity/engines/pitch_pitch_similarity.py",
            "similarity/engines/situation_similarity.py",
            "similarity/engines/batted_ball_similarity.py",
            "similarity/engines/*",
            "similarity/similarity_calibration.py",
            "similarity/calibration/fitting.py",
            "similarity/calibration/report.py",
            "similarity/calibration/base.py",
            "similarity/calibration/*",
            "similarity/similarity_diagnostics.py",
            "similarity/backtesting/*",
            "similarity/*",
        ],
    ),
    (
        6,
        "Profiles and pools: the DuckDB layer",
        "The nightly computor that turns raw pitches into player profiles and pools of real plays.",
        [
            "pipeline/batch/player_profile_computor.py",
            "pipeline/batch/profiles/base.py",
            "pipeline/batch/profiles/constants.py",
            "pipeline/batch/profiles/sql.py",
            "pipeline/batch/profiles/pitcher.py",
            "pipeline/batch/profiles/gmm.py",
            "pipeline/batch/profiles/batter.py",
            "pipeline/batch/profiles/fielder.py",
            "pipeline/batch/profiles/fielder_plays.py",
            "pipeline/batch/profiles/defense.py",
            "pipeline/batch/profiles/catcher.py",
            "pipeline/batch/profiles/baserunner.py",
            "pipeline/batch/profiles/manager.py",
            "pipeline/batch/profiles/park.py",
            "pipeline/batch/profiles/leverage.py",
            "pipeline/batch/profiles/league_averages.py",
            "pipeline/batch/profiles/pools.py",
            "pipeline/batch/profiles/pool_meta.py",
            "pipeline/batch/profiles/*",
            "pipeline/batch/*",
            "db/schemas/02_duckdb_schema.sql",
            "db/schemas/duckdb_schema_version.txt",
            "db/migrations/duckdb/*",
        ],
    ),
    (
        7,
        "Getting data in: the raw tables",
        "The loaders that pull MLB, Statcast and Savant data into the Postgres raw tables.",
        [
            "db/schemas/01_postgres_schema.sql",
            "pipeline/statcast_events.py",
            "pipeline/etl/etl_historical_loader.py",
            "pipeline/etl/historical/base.py",
            "pipeline/etl/historical/schedule.py",
            "pipeline/etl/historical/game_values.py",
            "pipeline/etl/historical/rows.py",
            "pipeline/etl/historical/reference_rows.py",
            "pipeline/etl/historical/venues.py",
            "pipeline/etl/historical/savant.py",
            "pipeline/etl/historical/writes.py",
            "pipeline/etl/historical/*",
            "pipeline/etl/play_events.py",
            "pipeline/etl/boxscore_ingest.py",
            "pipeline/etl/coercion.py",
            "pipeline/etl/savant_loader.py",
            "pipeline/etl/savant_boards.py",
            "pipeline/etl/savant_pitch_tracking_loader.py",
            "pipeline/etl/etl_sprint_speed_loader.py",
            "pipeline/etl/mlb_venue_dimensions.py",
            "pipeline/etl/venue_backfill_job.py",
            "pipeline/etl/*",
            "pipeline/bullpen_usage.py",
            "pipeline/db_dsn.py",
            "db/introspect.py",
            "alembic.ini",
            "db/migrations/*",
            "pipeline/*",
            "db/*",
        ],
    ),
    (
        8,
        "Running many games and reading the results",
        "The worker pool and the code that turns 100 games into win odds, prop "
        "distributions and market prices.",
        [
            "simulation/sim_kwargs.py",
            "simulation/lineup_resolver.py",
            "simulation/production_factory.py",
            "simulation/batch_runner.py",
            "simulation/results.py",
            "simulation/linescore.py",
            "simulation/pitcher_decisions.py",
            "simulation/prop_distributions.py",
            "simulation/game_market_distributions.py",
            "simulation/win_probability.py",
            "simulation/whatif.py",
            "simulation/play_recorder.py",
            "simulation/snapshots.py",
            "simulation/cache_codec.py",
            "simulation/prop_validation.py",
            "simulation/validation/*",
            "simulation/*",
            "db/sim_store.py",
        ],
    ),
    (
        9,
        "Odds and betting",
        "Where the bookmakers' prices come from, and how the model's numbers become edges.",
        [
            "pipeline/odds_provider.py",
            "pipeline/bettingpros_odds_provider.py",
            "pipeline/bettingpros/*",
            "pipeline/odds_row_guard.py",
            "pipeline/etl/opening_line_job.py",
            "betting/clv_engine.py",
            "betting/bet_signal.py",
            "betting/line_movement.py",
            "betting/*",
        ],
    ),
    (
        10,
        "The API",
        "The FastAPI app: its boot, the login, the routes the frontend calls and the run jobs.",
        [
            "api/main.py",
            "api/startup.py",
            "api/env_check.py",
            "api/middleware.py",
            "api/state.py",
            "api/auth.py",
            "api/errors.py",
            "api/serialization.py",
            "api/sim_jobs.py",
            "api/bundle_watch.py",
            "api/routes/__init__.py",
            "api/routes/_common.py",
            "api/routes/games.py",
            "api/routes/games_slate.py",
            "api/routes/games_feed.py",
            "api/routes/games_runs.py",
            "api/routes/games_models.py",
            "api/routes/betting.py",
            "api/routes/betting_pricing.py",
            "api/routes/betting_stored_odds.py",
            "api/routes/betting_lines.py",
            "api/routes/betting_card.py",
            "api/routes/auth.py",
            "api/routes/metrics.py",
            "api/schemas/__init__.py",
            "api/schemas/_base.py",
            "api/schemas/*",
            "api/routes/similarity.py",
            "api/routes/similarity_explorer.py",
            "api/routes/*",
            "api/*",
        ],
    ),
    (
        11,
        "Game day: the live service",
        "The service that follows today's games, writes the posted lineups and pushes updates.",
        [
            "pipeline/mlb_schedule.py",
            "pipeline/mlb_game_feed.py",
            "api/league_feed.py",
            "pipeline/live/run_live.py",
            "pipeline/live/live_ingestion_pipeline.py",
            "pipeline/live/ingestion/base.py",
            "pipeline/live/ingestion/constants.py",
            "pipeline/live/ingestion/mlb_websocket.py",
            "pipeline/live/ingestion/game_state_builder.py",
            "pipeline/live/ingestion/*",
            "pipeline/live/lineup_writer.py",
            "pipeline/live/broadcast.py",
            "pipeline/live/bullpen_availability_ingest.py",
            "pipeline/live/*",
        ],
    ),
    (
        12,
        "The frontend",
        "The React app: the day slate, the game page, the Data Lab and the similarity explorer.",
        [
            "frontend/index.html",
            "frontend/src/main.tsx",
            "frontend/src/App.tsx",
            "frontend/src/contexts/*",
            "frontend/src/api/http.ts",
            "frontend/src/api/typed.ts",
            "frontend/src/api/*",
            "frontend/src/pages/DaySummaryPage.tsx",
            "frontend/src/components/slate/*",
            "frontend/src/pages/GamePage.tsx",
            "frontend/src/components/games/*",
            "frontend/src/components/graphics/*",
            "frontend/src/hooks/*",
            "frontend/src/pages/*",
            "frontend/src/components/*",
            "frontend/src/styles/*",
            "frontend/src/*",
            "frontend/e2e/*",
            "frontend/*",
        ],
    ),
    (
        13,
        "Operations: the stack, the nightly jobs, the backups",
        "How the services run, what runs every night, and how the data is backed up.",
        [
            "docker-compose.yml",
            "Dockerfile",
            "Makefile",
            ".env.example",
            ".env.staging.example",
            ".env.production.example",
            "deploy/nginx/*",
            "deploy/ofelia/*",
            "scripts/with_mark.sh",
            "scripts/with_retry.sh",
            "scripts/wait_for_ingest.sh",
            "scripts/nightly_finals.sh",
            "scripts/nightly_ingest.sh",
            "scripts/nightly_closing_lines.sh",
            "scripts/nightly_rebuild.sh",
            "scripts/weekly_refresh.sh",
            "pipeline/backup/*",
            "db/backup/*",
            "scripts/backup_drill.py",
            "db/roles/*",
            "scripts/gen_secrets.py",
            "deploy/monitoring/*",
            "deploy/*",
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-docs.txt",
            "mkdocs.yml",
            ".pre-commit-config.yaml",
            ".gitignore",
            ".gitattributes",
            ".dockerignore",
        ],
    ),
    (
        14,
        "Measurement and maintenance tools",
        "The scripts that measure the simulator, rebuild the pools and backfill data.",
        [
            "scripts/sim_stats.py",
            "scripts/balanced_game_set.py",
            "scripts/accuracy_comparison.py",
            "scripts/accuracy/*",
            "scripts/pair_accuracy.py",
            "scripts/designed_experiment.py",
            "scripts/designed_experiment_run.sh",
            "scripts/fence_check.py",
            "scripts/pool_window_census.py",
            "scripts/validate_props.py",
            "scripts/fit_calibration.py",
            "scripts/calibrate_markets.py",
            "scripts/market_skill.py",
            "scripts/offline_draw_fit.py",
            "scripts/rebuild_pitch_pool.py",
            "scripts/pitch_pool_rebuild/*",
            "scripts/rebuild_steal_pool.py",
            "scripts/steal_pool_rebuild/*",
            "scripts/load_historical_odds.py",
            "scripts/load_official_boxscores.py",
            "scripts/reload_games.py",
            "scripts/resumable_sweep.py",
            "scripts/behaviour_fingerprint.py",
            "scripts/*",
            "scripts/probes/*",
        ],
    ),
    (
        15,
        "Tests and CI",
        "How the code is tested, and what CI runs on every push.",
        [
            "tests/conftest.py",
            "tests/script_loader.py",
            "tests/__init__.py",
            "tests/regression/*",
            "tests/acceptance/*",
            "tests/unit/*",
            "tests/integration/*",
            "tests/performance/*",
            "tests/fixtures/*",
            "tests/*",
            ".github/workflows/ci.yml",
            ".github/*",
        ],
    ),
]

#: Source and config files by suffix. Every other suffix is data or a document.
_SOURCE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".mjs", ".cjs", ".js", ".sql", ".sh", ".yml", ".yaml",
    ".ini", ".toml", ".conf", ".css", ".html", ".mako",
}  # fmt: skip
#: Source and config files by exact path or name.
_SOURCE_NAMES = {
    "Dockerfile", "Makefile", "frontend/package.json", "frontend/tsconfig.json",
    "frontend/tsconfig.node.json", "frontend/openapi.json", "db/schemas/duckdb_schema_version.txt",
    "requirements.txt", "requirements-dev.txt", "requirements-docs.txt",
    ".env.example", ".env.staging.example", ".env.production.example", ".gitignore",
    ".gitattributes", ".dockerignore", "frontend/Dockerfile.dockerignore",
    "deploy/monitoring/grafana/baseball_sim_dashboard.json",
    "deploy/monitoring/secrets/app_api_key.example",
}  # fmt: skip
#: Fixed descriptions for files that carry no comment of their own.
_FIXED = {
    "frontend/package.json": "The frontend's npm manifest: its dependencies and its scripts (dev, build, lint, e2e, gen:api).",
    "frontend/tsconfig.json": "The TypeScript compiler settings for the frontend source.",
    "frontend/tsconfig.node.json": "The TypeScript settings for the Vite config file.",
    "frontend/openapi.json": "The API contract snapshot (generated by scripts/export_openapi.py; the frontend's types come from it).",
    "frontend/src/api/schema.d.ts": "The TypeScript types of every API route and response (generated from openapi.json by `npm run gen:api`).",
    "db/schemas/duckdb_schema_version.txt": "The DuckDB schema version: the number of the newest file under db/migrations/duckdb/.",
    "deploy/monitoring/grafana/baseball_sim_dashboard.json": "The Grafana dashboard of the app's metrics.",
    "deploy/monitoring/secrets/app_api_key.example": "An example of the API key file Prometheus reads to scrape the app.",
    "requirements.txt": "The Python packages the app image installs.",
    "requirements-dev.txt": "The extra Python packages for tests, lint and type checks.",
    "requirements-docs.txt": "The packages that build the technical reference website (MkDocs).",
    "frontend/Dockerfile.dockerignore": "The files the frontend image build leaves out.",
    "frontend/index.html": "The HTML page that loads the React app.",
    "frontend/src/main.tsx": "The frontend's entry point: it loads the bundled fonts and the styles, then mounts the React app.",
    "frontend/vite.config.ts": "The Vite build and dev-server settings, including the proxy to the API.",
    "frontend/.eslintrc.cjs": "The ESLint rules for the frontend source.",
    "mkdocs.yml": "The MkDocs settings that build the technical reference website from docs/technical/.",
    "db/migrations/script.py.mako": "The template Alembic copies for a new migration file.",
}


@dataclass(slots=True)
class Row:
    """One file of the index."""

    path: str
    stage: int
    order: int
    lines: int
    text: str
    styles: bool = False
    tests: list[str] = field(default_factory=list)


def tracked_files() -> list[str]:
    """Every tracked path that exists on disk, from ``git ls-files``."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return sorted(p for p in out.split("\0") if p and (REPO / p).is_file())


def is_source(path: str) -> bool:
    """True for a source or config file the index lists."""
    if path.startswith("docs/") or "node_modules/" in path:
        return False
    if path in _SOURCE_NAMES or Path(path).name in _SOURCE_NAMES:
        return True
    if path.startswith("tests/fixtures/") and not path.endswith(".py"):
        return False
    return Path(path).suffix in _SOURCE_SUFFIXES


def _literal_prefix(pattern: str) -> int:
    """The length of a pattern's literal head, before its first wildcard."""
    cut = min((i for i, ch in enumerate(pattern) if ch in "*?["), default=len(pattern))
    return cut


def place(path: str) -> tuple[int, int] | None:
    """(stage, pattern order) of the most specific pattern that matches the path."""
    best: tuple[int, int, int] | None = None  # (prefix length, stage, order)
    for stage, _title, _summary, patterns in STAGES:
        for order, pattern in enumerate(patterns):
            # A "dir/*" pattern matches every file under that folder, at any depth;
            # a deeper pattern is more specific and wins.
            if pattern.endswith("/*"):
                folder = pattern[:-2]
                hit = path.startswith(folder + "/")
            else:
                hit = fnmatch.fnmatchcase(path, pattern)
            if not hit:
                continue
            score = _literal_prefix(pattern)
            if best is None or score > best[0]:
                best = (score, stage, order)
    return None if best is None else (best[1], best[2])


# --- descriptions -----------------------------------------------------------------


def _first_sentence(text: str) -> str:
    """The first sentence of the first paragraph, on one line, at most 220 characters."""
    para = text.strip().split("\n\n", 1)[0]
    para = " ".join(para.split())
    para = para.replace("``", "`")
    match = re.match(r"(.+?[.!?])(?=\s|$)", para)
    sentence = match.group(1) if match else para
    if len(sentence) > 220:
        sentence = sentence[:217].rstrip() + "..."
    return sentence


def _headline(text: str) -> str:
    """A header comment's title line alone, when it is a title (no full stop) and
    the next line starts a new thought."""
    lines = text.strip().splitlines()
    if len(lines) > 1 and not re.search(r"[.!?:,;]$", lines[0]):
        nxt = lines[1].strip()
        if not nxt or nxt[0].isupper() or nxt.startswith("SIM-"):
            return lines[0]
    return text


def _python_text(source: str) -> str:
    """The module docstring, less a leading reST title block (a title line over a
    line of ``=`` or ``-``)."""
    try:
        doc = ast.get_docstring(ast.parse(source))
    except SyntaxError:
        return ""
    lines = (doc or "").strip().splitlines()
    if len(lines) >= 2 and re.fullmatch(r"[=\-~^*]{3,}", lines[1].strip()):
        lines = lines[2:]
    return "\n".join(lines).strip()


def _comment_text(source: str, marker: str) -> str:
    """The leading block of ``marker`` comment lines (after a shebang or blank lines)."""
    lines: list[str] = []
    for raw in source.splitlines():
        line = raw.strip()
        if not lines and (not line or line.startswith("#!")):
            continue
        if line.startswith(marker):
            lines.append(line[len(marker) :].strip().lstrip("#-=").strip())
            continue
        break
    # Drop decorative rules and empty lines at the head.
    while lines and not re.search(r"[A-Za-z]", lines[0]):
        lines.pop(0)
    return "\n".join(lines)


def _block_comment_text(source: str) -> str:
    """The first ``/* ... */`` or ``//`` comment before the first declaration."""
    match = re.search(r"/\*\*?(.*?)\*/", source, flags=re.DOTALL)
    head_end = re.search(
        r"^\s*(export|function|const|let|class|interface|type|@|:root|body|html)\b",
        source,
        flags=re.MULTILINE,
    )
    limit = head_end.start() if head_end else len(source)
    if match and match.start() < limit:
        body = "\n".join(line.strip().lstrip("*").strip() for line in match.group(1).splitlines())
        return body.strip()
    slashes = _comment_text(source[:limit], "//")
    return slashes


def describe(path: str, source: str) -> str:
    """The file's purpose: the first sentence of its docstring or header comment."""
    if path in _FIXED:
        return _FIXED[path]
    suffix = Path(path).suffix
    name = Path(path).name
    if suffix == ".py":
        text = _python_text(source)
        if not text and name == "__init__.py":
            return "Package marker."
    elif suffix == ".sql":
        text = _comment_text(source, "--")
    elif suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs", ".css"}:
        text = _block_comment_text(source)
    elif suffix == ".html":
        text = ""
    elif suffix == ".ini":
        text = _comment_text(source, ";") or _comment_text(source, "#")
    else:
        text = _comment_text(source, "#")
    if suffix != ".py":
        text = _headline(text)
    sentence = _first_sentence(text) if text else ""
    return sentence or "(no description: the file has no docstring or header comment)"


# --- which tests import a module ---------------------------------------------------


def module_keys(path: str) -> set[str]:
    """The names a test uses to import the file at ``path``."""
    if not path.endswith(".py"):
        return set()
    parts = path[:-3].split("/")
    if parts[0] == "scripts":
        rest = parts[1:]
        if rest and rest[0] == "probes":
            return {rest[-1]}
        if rest and rest[-1] == "__init__":
            rest = rest[:-1]
        return {".".join(rest)} if rest else set()
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join(parts)}


def test_imports(paths: list[str]) -> dict[str, set[str]]:
    """{test file: the module names it imports or loads}."""
    found: dict[str, set[str]] = {}
    for path in paths:
        name = Path(path).name
        if not path.startswith("tests/") or not (
            name.startswith("test_") or name.startswith("bench_")
        ):
            continue
        try:
            tree = ast.parse((REPO / path).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", "")) == "load_script"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(Path(node.args[0].value).stem)
        found[path] = names
    return found


def _rank_tests(path: str, tests: list[str]) -> list[str]:
    """The tests that import a file, the ones named after it first."""
    stem = Path(path).stem.replace("_similarity", "")
    return sorted(tests, key=lambda t: (stem not in Path(t).stem, len(Path(t).stem), t))


# --- the page ----------------------------------------------------------------------


def build_rows() -> tuple[list[Row], list[str]]:
    """The index rows, and the source files no stage claims."""
    paths = tracked_files()
    sources = [p for p in paths if is_source(p)]
    tsx = {p[: -len(".tsx")] for p in sources if p.endswith(".tsx")}
    imports = test_imports(paths)
    rows: list[Row] = []
    unplaced: list[str] = []
    styled: set[str] = set()
    for path in sources:
        # A component's own CSS module rides on the component's row.
        if path.endswith(".module.css") and path[: -len(".module.css")] in tsx:
            styled.add(path[: -len(".module.css")] + ".tsx")
            continue
        spot = place(path)
        if spot is None:
            unplaced.append(path)
            continue
        source = (REPO / path).read_text(encoding="utf-8", errors="replace")
        keys = module_keys(path)
        tests = [t for t, names in imports.items() if keys & names and t != path]
        rows.append(
            Row(
                path=path,
                stage=spot[0],
                order=spot[1],
                lines=len(source.splitlines()),
                text=describe(path, source),
                tests=_rank_tests(path, tests),
            )
        )
    for row in rows:
        row.styles = row.path in styled
    rows.sort(key=lambda r: (r.stage, r.order, r.path))
    return rows, unplaced


def index_key(rows: list[Row]) -> str:
    """The hash of each row's stage, path and first sentence."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{row.stage}|{row.path}|{row.text}\n".encode())
    return digest.hexdigest()[:16]


def _cell(text: str) -> str:
    """Text safe inside a Markdown table cell: pipes escaped, angle brackets outside
    code spans escaped."""
    pieces = text.split("`")
    for i in range(0, len(pieces), 2):  # the even pieces are outside code spans
        pieces[i] = pieces[i].replace("<", "&lt;").replace(">", "&gt;")
    return "`".join(pieces).replace("|", "\\|")


def _tests_cell(row: Row) -> str:
    if not row.tests:
        return "—"
    shown = ", ".join(f"`{Path(t).name}`" for t in row.tests[:3])
    more = len(row.tests) - 3
    return f"{len(row.tests)}: {shown}" + (f" +{more}" if more > 0 else "")


def render(rows: list[Row]) -> str:
    """The Markdown page."""
    titles = {stage: (title, summary) for stage, title, summary, _ in STAGES}
    out = [
        f"{KEY_PREFIX}{index_key(rows)} -->",
        "# File index",
        "",
        "Every source and config file in the repository, in the order of the "
        "[code review path](code-review-path.md). Data files (test fixtures, script "
        "inputs, logos, fonts) and the documents are not listed.",
        "",
        "- `scripts/docs_file_index.py` writes this page from `git ls-files`. Do not edit "
        "it by hand. Regenerate it with `python scripts/docs_file_index.py`.",
        "- **What it is** is the first sentence of the file's own docstring or header "
        "comment. A poor description needs a better docstring, not an edit here.",
        "- **Tested by** counts the test files that import the file, and names the most "
        "specific three.",
        "- A component's CSS module rides on the component's row (marked *+ css*).",
        "- `tests/unit/test_docs_file_index.py` fails when a file is added, removed or "
        "moved to another stage, or its first sentence changes. The line counts and "
        "the test lists refresh whenever the page is written.",
        "",
        "| Stage | Files | Lines |",
        "|---|--:|--:|",
    ]
    for stage, (title, _summary) in titles.items():
        members = [r for r in rows if r.stage == stage]
        lines = sum(r.lines for r in members)
        out.append(f"| [{stage}. {title}](#stage-{stage}) | {len(members)} | {lines:,} |")
    total = sum(r.lines for r in rows)
    out.append(f"| **All** | **{len(rows)}** | **{total:,}** |")
    for stage, (title, summary) in titles.items():
        members = [r for r in rows if r.stage == stage]
        out += [
            "",
            f'<a id="stage-{stage}"></a>',
            "",
            f"## {stage}. {title}",
            "",
            f"{summary} The [review path](code-review-path.md#stage-{stage}) says what to "
            "look for.",
            "",
            "| File | Lines | What it is | Tested by |",
            "|---|--:|---|---|",
        ]
        for row in members:
            css = " *+ css*" if row.styles else ""
            link = f"[`{row.path}`](../../{row.path}){css}"
            out.append(f"| {link} | {row.lines:,} | {_cell(row.text)} | {_tests_cell(row)} |")
    return "\n".join(out) + "\n"


def committed_key(text: str) -> str | None:
    """The key on the first line of a written index, or None."""
    first = text.split("\n", 1)[0]
    if first.startswith(KEY_PREFIX) and first.endswith(" -->"):
        return first[len(KEY_PREFIX) : -len(" -->")]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when the page is stale")
    args = parser.parse_args(argv)
    rows, unplaced = build_rows()
    if unplaced:
        print("No review stage claims these files; add a pattern to STAGES:", file=sys.stderr)
        for path in unplaced:
            print(f"  {path}", file=sys.stderr)
        return 1
    if args.check:
        current = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
        if committed_key(current) != index_key(rows):
            print(f"{INDEX.relative_to(REPO)} is stale: run python scripts/docs_file_index.py")
            return 1
        print(f"{INDEX.relative_to(REPO)} is current ({len(rows)} files).")
        return 0
    INDEX.write_text(render(rows), encoding="utf-8", newline="\n")
    print(f"Wrote {INDEX.relative_to(REPO)}: {len(rows)} files in {len(STAGES)} stages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
