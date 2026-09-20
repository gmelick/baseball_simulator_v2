"""
SIM-533 — the pitcher arm-angle and spin-shape decision: the guard.

The owner decided on 2026-09-19 that neither of Savant's two numbers joins
the pitcher model: not the arm angle (the release point measured from the
shoulder), and not the spin shape (active spin and the measured-minus-inferred
spin-axis deviation). The arsenal already reads the release point as three of
its eight dimensions, and the spin numbers are formulas on the other five.
The build is the record only: a decision block beside ``GMM_FEATURE_NAMES``
in the pitcher engine, a paragraph in each of the two technical documents,
and a probe script that re-runs the numbers.

Plan: docs/audit/2026-09-18-sim533-pitcher-arm-angle-spin-shape-plan.md (§7).

What these tests hold:

* the decision stays honest: ``GMM_FEATURE_NAMES`` and ``COMMAND_FEATURES``
  name none of the two numbers, and the arsenal's list is exactly the eight
  dimensions that include the release point (the ticket's premise);
* the engine carries the decision block, naming the plan, within 40 lines
  above the line that defines ``GMM_FEATURE_NAMES``, so a later edit that
  moves or drops the block fails here;
* the two technical documents carry the record, and the reference's entry
  sits inside the pitcher engine's own section;
* no profile column reads the two numbers, no board landing table exists
  (canonical DDL and every Alembic migration), no Savant board is registered
  for them (the plan's decision 3), and the per-pitch ``arm_angle`` that
  migration 0020 carried into ``raw.savant_pitch_tracking`` for this ticket
  stays unread by the profile computor, the engines and the artifact builder;
* the probe's offline checks pass on the bundled fixture, in process and as
  a subprocess, and the first check reads Savant's formula at r >= 0.999;
* the probe's offline checks can fail: a corrupted spin-axis convention
  trips the convention check, and a zero-angle arm formula trips the formula
  check, so the checks are not vacuous.

No live database, no network.
"""

from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
import tokenize
import warnings
from pathlib import Path
from types import ModuleType

import pytest

import similarity.engines.pitcher_similarity as pitcher_similarity
from pipeline.etl.savant_boards import BOARDS

REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "similarity" / "engines" / "pitcher_similarity.py"
SIMILARITY_MD = REPO / "docs" / "technical" / "similarity.md"
CHEAT_SHEET_MD = REPO / "docs" / "technical" / "sim-loop-cheat-sheet.md"
DUCKDB_SCHEMA = REPO / "db" / "schemas" / "02_duckdb_schema.sql"
POSTGRES_SCHEMA = REPO / "db" / "schemas" / "01_postgres_schema.sql"
PROBE = REPO / "scripts" / "sim533_arm_angle_probe.py"
PLAN_PATH = "docs/audit/2026-09-18-sim533-pitcher-arm-angle-spin-shape-plan.md"

#: The eight arsenal dimensions. The release point is the last three.
EXPECTED_GMM_FEATURES = [
    "velo",
    "ivb",
    "hb",
    "spin_rate",
    "spin_axis",
    "release_x",
    "release_z",
    "release_ext",
]
#: Substrings that would name one of the two numbers as a feature.
FORBIDDEN_FEATURE_SUBSTRINGS = (
    "arm_angle",
    "ball_angle",
    "active_spin",
    "spin_deviation",
    "axis_deviation",
    "shoulder",
)
#: Substrings that would name a profile column for them (the DuckDB profile tables).
FORBIDDEN_PROFILE_SUBSTRINGS = ("arm_angle", "active_spin", "spin_direction", "axis_deviation")
#: A board landing table the plan declines to create (the alternative path's Alembic 0028).
BOARD_TABLE_PATTERN = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:raw\.)?savant_"
    r"(?:pitcher_)?(?:arm_angles?|spin_direction|active_spin)\b",
    re.IGNORECASE,
)
#: URL fragments of the three Savant boards the plan declines to register.
FORBIDDEN_BOARD_URL_FRAGMENTS = ("pitcher-arm-angles", "spin-direction", "active-spin")
#: The readers that must not touch the per-pitch ``arm_angle`` column.
UNREADING_MODULES = (
    REPO / "pipeline" / "batch" / "player_profile_computor.py",
    REPO / "pipeline" / "batch" / "engine_artifacts.py",
    *sorted((REPO / "similarity" / "engines").glob("*.py")),
)
ALEMBIC_VERSIONS = REPO / "db" / "migrations" / "versions"
#: How far above ``GMM_FEATURE_NAMES`` the decision block must start.
BLOCK_MAX_LINES_ABOVE = 40
PITCHER_HEADING = "### `similarity/engines/pitcher_similarity.py`"


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    """The probe script loaded as a module (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("sim533_arm_angle_probe", PROBE)
    assert spec is not None and spec.loader is not None, f"cannot load {PROBE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _failed(checks: list[tuple[str, bool, str]]) -> list[str]:
    return [name for name, ok, _ in checks if not ok]


# ---------------------------------------------------------------------------
# 1. The feature lists
# ---------------------------------------------------------------------------


def test_feature_lists_name_no_arm_angle_or_spin_shape() -> None:
    """Neither feature list names the arm angle or the spin shape, and the
    arsenal reads exactly the eight dimensions, release point included."""
    for list_name in ("GMM_FEATURE_NAMES", "COMMAND_FEATURES"):
        features = getattr(pitcher_similarity, list_name)
        for feature in features:
            lowered = str(feature).lower()
            hits = [s for s in FORBIDDEN_FEATURE_SUBSTRINGS if s in lowered]
            assert not hits, (
                f"{list_name} names {feature!r}, which matches {hits}: the SIM-533 decision "
                f"says these are not features; edit the decision block in {ENGINE.name} first"
            )
    assert list(pitcher_similarity.GMM_FEATURE_NAMES) == EXPECTED_GMM_FEATURES, (
        "GMM_FEATURE_NAMES must be the eight arsenal dimensions "
        f"{EXPECTED_GMM_FEATURES}; got {list(pitcher_similarity.GMM_FEATURE_NAMES)}"
    )


# ---------------------------------------------------------------------------
# 2. The decision block in the engine
# ---------------------------------------------------------------------------


def test_engine_carries_the_decision_block() -> None:
    """The engine's source carries the SIM-533 block, naming the plan, within
    40 lines above the line that defines ``GMM_FEATURE_NAMES``."""
    lines = ENGINE.read_text(encoding="utf-8").splitlines()

    definition_lines = [
        i for i, line in enumerate(lines) if re.match(r"^GMM_FEATURE_NAMES\s*=", line)
    ]
    assert len(definition_lines) == 1, (
        f"expected exactly one 'GMM_FEATURE_NAMES =' line in {ENGINE.name}; "
        f"found {len(definition_lines)}"
    )
    definition = definition_lines[0]

    block_starts = [
        i
        for i, line in enumerate(lines[:definition])
        if line.lstrip().startswith("#") and "SIM-533" in line
    ]
    assert block_starts, (
        f"{ENGINE.name} has no '# SIM-533' comment above GMM_FEATURE_NAMES "
        "(the decision block is gone)"
    )
    block_start = block_starts[-1]
    distance = definition - block_start
    assert 0 < distance <= BLOCK_MAX_LINES_ABOVE, (
        f"the SIM-533 decision block starts {distance} lines above GMM_FEATURE_NAMES "
        f"(line {block_start + 1} vs {definition + 1}); it must sit within "
        f"{BLOCK_MAX_LINES_ABOVE} lines above the definition"
    )

    block = "\n".join(lines[block_start:definition])
    assert PLAN_PATH in block, f"the decision block does not name the plan {PLAN_PATH}"
    assert "are NOT features" in block, (
        "the decision block does not say the two numbers 'are NOT features'"
    )


# ---------------------------------------------------------------------------
# 3. The two technical documents
# ---------------------------------------------------------------------------


def test_the_two_technical_documents_carry_the_record() -> None:
    """Both documents mention SIM-533, and the reference's mention lies inside
    the pitcher engine's section."""
    cheat_sheet = CHEAT_SHEET_MD.read_text(encoding="utf-8")
    assert "SIM-533" in cheat_sheet, f"{CHEAT_SHEET_MD.name} carries no SIM-533 record"

    reference = SIMILARITY_MD.read_text(encoding="utf-8")
    assert "SIM-533" in reference, f"{SIMILARITY_MD.name} carries no SIM-533 record"

    start = reference.find(PITCHER_HEADING)
    assert start >= 0, f"{SIMILARITY_MD.name} has no heading {PITCHER_HEADING!r}"
    next_heading = reference.find("\n### ", start + len(PITCHER_HEADING))
    section = reference[start:] if next_heading < 0 else reference[start:next_heading]
    assert "SIM-533" in section, (
        f"{SIMILARITY_MD.name} mentions SIM-533, but not inside the pitcher engine's "
        f"section (between {PITCHER_HEADING!r} and the next '### ' heading)"
    )


# ---------------------------------------------------------------------------
# 4. No column, no table, no board
# ---------------------------------------------------------------------------


def _without_sql_comments(text: str) -> str:
    """The DDL with ``--`` line comments and ``/* */`` blocks removed, so a
    comment that mentions a word does not read as a column."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", text)


def _without_python_comments(source: str) -> str:
    """The module's source with every ``#`` comment token removed; strings
    stay, so a SQL ``SELECT`` of a column still reads as a reader."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return tokenize.untokenize(
        (tok.type, "" if tok.type == tokenize.COMMENT else tok.string) for tok in tokens
    )


def test_no_profile_column_or_raw_table_for_the_two_numbers() -> None:
    """No profile column, no board landing table, no registered board, and the
    per-pitch ``arm_angle`` column stays unread (the plan's decision 3)."""
    profile_ddl = _without_sql_comments(DUCKDB_SCHEMA.read_text(encoding="utf-8")).lower()
    hits = [s for s in FORBIDDEN_PROFILE_SUBSTRINGS if s in profile_ddl]
    assert not hits, (
        f"{DUCKDB_SCHEMA.relative_to(REPO)} names {hits}: SIM-533 records no profile "
        "column for the arm angle or the spin shape"
    )

    for ddl in (POSTGRES_SCHEMA, *sorted(ALEMBIC_VERSIONS.glob("*.py"))):
        match = BOARD_TABLE_PATTERN.search(ddl.read_text(encoding="utf-8"))
        assert match is None, (
            f"{ddl.relative_to(REPO)} creates {match.group(0)!r}: SIM-533 records no "
            "landing table for the arm-angle, spin-direction or active-spin boards"
        )

    # Migration 0020 carried Savant's per-pitch arm angle into
    # raw.savant_pitch_tracking for this ticket. It stays stored and unread: no
    # reader names the column in code or in a SQL string (comments, such as the
    # decision block itself, are dropped before the check).
    for module in UNREADING_MODULES:
        code = _without_python_comments(module.read_text(encoding="utf-8"))
        assert "arm_angle" not in code, (
            f"{module.relative_to(REPO)} reads arm_angle: SIM-533 leaves the per-pitch "
            "column in raw.savant_pitch_tracking unread; edit the decision block first"
        )

    for name, board in BOARDS.items():
        url = str(board.url).lower()
        hits = [f for f in FORBIDDEN_BOARD_URL_FRAGMENTS if f in url]
        assert not hits, (
            f"BOARDS[{name!r}] registers {board.url}, which matches {hits}: SIM-533 records "
            "no load of the arm-angle, spin-direction or active-spin boards"
        )


# ---------------------------------------------------------------------------
# 5. The probe passes
# ---------------------------------------------------------------------------


def test_probe_offline_checks_pass(probe: ModuleType) -> None:
    """Every offline check passes on the bundled fixture, in process and as a
    subprocess; the first check reads Savant's formula at r >= 0.999."""
    checks = probe.offline_checks(probe.ARM_FIXTURE, probe.SPIN_FIXTURE)
    assert checks, "offline_checks returned no checks"
    assert not _failed(checks), f"offline checks failed: {_failed(checks)}; all: {checks}"

    first_name, first_ok, first_detail = checks[0]
    assert first_name == "savant_angle_formula", (
        f"the first check must be Savant's formula; got {first_name!r}"
    )
    match = re.search(r"\br (\d+\.\d+)", first_detail)
    assert match is not None, f"no 'r <value>' in the first check's detail: {first_detail!r}"
    assert float(match.group(1)) >= 0.999, (
        f"Savant's formula reads r {match.group(1)} on the fixture; the plan says r 1.000"
    )

    result = subprocess.run(
        [sys.executable, str(PROBE), "--offline"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"`{PROBE.name} --offline` exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "FAIL" not in result.stdout, f"`{PROBE.name} --offline` printed a FAIL:\n{result.stdout}"
    assert "OK savant_angle_formula" in result.stdout, (
        f"`{PROBE.name} --offline` did not print the formula check:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# 6. The probe can fail
# ---------------------------------------------------------------------------


def test_probe_offline_checks_can_fail(probe: ModuleType) -> None:
    """A corrupted fixture fails the check named for the corruption, so the
    checks are not vacuous."""
    # Rotate our spin axis by 90 degrees on every row: the mirrored
    # convention (360 minus ours) no longer matches Savant's measured axis.
    spin_rotated = [row[:14] + ((row[14] + 90.0) % 360.0,) for row in probe.SPIN_FIXTURE]
    checks = probe.offline_checks(probe.ARM_FIXTURE, spin_rotated)
    failed = _failed(checks)
    assert failed, "a 90-degree axis rotation failed no check: the checks are vacuous"
    assert "measured_axis_convention" in failed, (
        f"a 90-degree axis rotation must trip 'measured_axis_convention'; it tripped {failed}"
    )

    # Set every shoulder z to the release z: the rebuilt angle is zero on
    # every row (a constant column, so the correlation is undefined).
    arm_flat = [row[:7] + (row[5],) for row in probe.ARM_FIXTURE]
    with warnings.catch_warnings():
        # numpy warns on the correlation of a constant column; that is the
        # corruption's expected effect, not a defect under test.
        warnings.simplefilter("ignore", RuntimeWarning)
        checks = probe.offline_checks(arm_flat, probe.SPIN_FIXTURE)
    failed = _failed(checks)
    assert failed, "a zero-angle arm formula failed no check: the checks are vacuous"
    assert "savant_angle_formula" in failed, (
        f"a zero-angle arm formula must trip 'savant_angle_formula'; it tripped {failed}"
    )
