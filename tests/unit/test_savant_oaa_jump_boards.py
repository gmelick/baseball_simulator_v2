"""
SIM-532 — the loader side of the fielder's hand split and the outfield jump.

Two boards join the registry: Savant's Outs Above Average, pulled once per
position, and the Outfield Jump, pulled per player. The loader gains a fifth
season style (``startYear`` / ``endYear``) and a split pull whose parameter
name comes from the board (``pos`` here, ``pitchHand`` on the swing boards).

The tests that matter are the ones that guard the data: the OAA board's year
column is blank, so its per-row check cannot run and the probe is the only
guard; the figure is the fielder's AT the pulled position, so the position must
come from the query, never from the CSV's primary-position label.

No test here touches the network.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from pipeline.etl import savant_loader as sl
from pipeline.etl.savant_boards import BOARDS, FIELDING_BOARDS, PROBE_SEASON, SavantBoard

REPO = Path(__file__).resolve().parents[2]
ALEMBIC_0027 = REPO / "db" / "migrations" / "versions" / "0027_sim532_savant_oaa_and_jump.py"
PG_SCHEMA_SQL = REPO / "db" / "schemas" / "01_postgres_schema.sql"
FIELDER_ENGINE = REPO / "similarity" / "engines" / "fielder_similarity.py"
COMPUTOR = REPO / "pipeline" / "batch" / "player_profile_computor.py"

OAA = BOARDS["outs_above_average"]
JUMP = BOARDS["outfield_jump"]

POSITIONS = ("1B", "2B", "3B", "SS", "LF", "CF", "RF")

_OAA_CSV_HEADER = (
    "player_id,primary_pos_formatted,year,fielding_runs_prevented,outs_above_average,"
    "outs_above_average_infront,outs_above_average_lateral_toward3bline,"
    "outs_above_average_lateral_toward1bline,outs_above_average_behind,"
    "outs_above_average_rhh,outs_above_average_lhh\n"
)


def _oaa_csv_for(url: str) -> str:
    """A fake OAA board: one row per pull, the year column BLANK, the figure
    varying with the pulled position so a test can tell the pulls apart."""
    pos = re.search(r"[?&]pos=(\d)", url)
    code = int(pos.group(1)) if pos else 0
    return _OAA_CSV_HEADER + f"545361,CF,,{code * 2},{code},1,0,-1,2,{code - 1},1\n"


# ---------------------------------------------------------------------------
# The fifth season style
# ---------------------------------------------------------------------------


def test_the_startyear_style_sends_both_ends_of_the_range() -> None:
    assert OAA.season_params(2024) == {"startYear": "2024", "endYear": "2024"}
    url = sl.build_url(OAA, 2024)
    assert "startYear=2024" in url
    assert "endYear=2024" in url
    assert "csv=true" in url


def test_the_probe_asks_the_oaa_board_for_the_impossible_season() -> None:
    seen: list[str] = []
    sl.probe_season_param(OAA, fetcher=lambda url: seen.append(url) or _OAA_CSV_HEADER)
    assert f"startYear={PROBE_SEASON}" in seen[0]
    assert f"endYear={PROBE_SEASON}" in seen[0]


# ---------------------------------------------------------------------------
# The split pull takes its parameter name from the board
# ---------------------------------------------------------------------------


def test_a_split_pull_varies_the_board_s_own_parameter() -> None:
    """The swing boards send pitchHand and never pos; the OAA board sends pos
    and never pitchHand."""
    hand_url = sl.build_url(BOARDS["bat_tracking"], 2024, "L")
    assert "pitchHand=L" in hand_url
    assert "pos=" not in hand_url

    pos_url = sl.build_url(OAA, 2024, "8")
    assert "pos=8" in pos_url
    assert "pitchHand" not in pos_url


def test_an_empty_split_value_sends_no_split_parameter() -> None:
    url = sl.build_url(OAA, 2024, "")
    assert "pos=" not in url


def test_the_split_parameter_defaults_to_the_pitcher_s_hand() -> None:
    assert BOARDS["bat_tracking"].split_param == "pitchHand"
    assert BOARDS["swing_path"].split_param == "pitchHand"
    assert OAA.split_param == "pos"


def test_hand_splits_is_a_read_only_alias_of_splits() -> None:
    """The old name (SIM-529) still reads; the new name is the field."""
    board = BOARDS["bat_tracking"]
    assert board.hand_splits == board.splits
    assert board.splits == (("all", ""), ("vs_l", "L"), ("vs_r", "R"))
    alias = SavantBoard.hand_splits
    assert isinstance(alias, property) and alias.fset is None
    assert "hand_splits" not in {f.name for f in fields(SavantBoard)}


# ---------------------------------------------------------------------------
# The OAA board: seven pulls a season, the position from the query
# ---------------------------------------------------------------------------


def test_the_oaa_board_is_pulled_once_per_position() -> None:
    seen: list[str] = []

    def fetcher(url: str) -> str:
        seen.append(url)
        return _oaa_csv_for(url)

    n = sl.load_board_season(None, OAA, 2024, dry_run=True, fetcher=fetcher)
    assert len(seen) == 7
    assert [re.search(r"[?&]pos=(\d)", u).group(1) for u in seen] == list("3456789")
    assert all("pitchHand" not in u for u in seen)
    assert n == 7  # one row per position


def test_the_oaa_row_carries_the_position_from_the_query_not_the_csv() -> None:
    """The CSV's primary_pos_formatted is the player's PRIMARY position. The
    figure is the fielder's AT the pulled position (92 of 147 center-field rows
    of 2024 differ from the all-positions figure), so the key must be the
    position PULLED."""
    rows: list[dict] = []
    for label, value in OAA.splits:
        body = _oaa_csv_for(sl.build_url(OAA, 2024, value))
        for raw in sl.parse_rows(body):
            coerced = sl.coerce_row(OAA, raw, 2024, label)
            assert coerced is not None
            rows.append(coerced)
    assert [r["position"] for r in rows] == list(POSITIONS)
    assert all(r["primary_position"] == "CF" for r in rows)
    assert all(r["player_id"] == 545361 and r["season"] == 2024 for r in rows)
    # The figure differs pull by pull: the loader kept each position's own row.
    assert [r["outs_above_average"] for r in rows] == [3, 4, 5, 6, 7, 8, 9]


def test_the_position_splits_map_labels_to_savant_s_position_codes() -> None:
    assert OAA.splits == (
        ("1B", "3"),
        ("2B", "4"),
        ("3B", "5"),
        ("SS", "6"),
        ("LF", "7"),
        ("CF", "8"),
        ("RF", "9"),
    )
    assert OAA.split_column == "position"


# ---------------------------------------------------------------------------
# The season guards on the two boards
# ---------------------------------------------------------------------------


def test_the_oaa_board_has_no_season_column_so_the_row_check_is_skipped() -> None:
    """The board's year column is BLANK on every row. The probe is its only guard."""
    assert OAA.season_column is None
    sl.check_row_seasons(OAA, [{"player_id": "1", "year": ""}], 2024)
    sl.check_row_seasons(OAA, [{"player_id": "1", "year": "2026"}], 2024)  # unread


def test_the_jump_board_s_row_check_raises_on_a_wrong_year() -> None:
    assert JUMP.season_column == "year"
    rows = [{"resp_fielder_id": "1", "year": "2026"}, {"resp_fielder_id": "2", "year": "2024"}]
    with pytest.raises(sl.SeasonParamError, match="2026"):
        sl.check_row_seasons(JUMP, rows, 2024)
    sl.check_row_seasons(JUMP, [{"resp_fielder_id": "2", "year": "2024"}], 2024)


def test_the_jump_board_is_pulled_once_with_the_year_style() -> None:
    seen: list[str] = []

    def fetcher(url: str) -> str:
        seen.append(url)
        return (
            "resp_fielder_id,year,n,n_outs,outs_above_average,rel_league_reaction_distance,"
            "rel_league_burst_distance,rel_league_routing_distance,rel_league_bootup_distance,"
            "f_bootup_distance\n"
            "545361,2024,64,50,4,1.2,0.4,-0.3,1.3,36.5\n"
        )

    n = sl.load_board_season(None, JUMP, 2024, dry_run=True, fetcher=fetcher)
    assert len(seen) == 1
    assert "year=2024" in seen[0]
    assert "min=0" in seen[0]
    assert "pos=" not in seen[0] and "pitchHand" not in seen[0]
    assert n == 1


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def test_the_oaa_count_columns_coerce_to_integers() -> None:
    raw = next(iter(sl.parse_rows(_oaa_csv_for("x?pos=8"))))
    out = sl.coerce_row(OAA, raw, 2024, "CF")
    assert out == {
        "player_id": 545361,
        "season": 2024,
        "position": "CF",
        "primary_position": "CF",
        "fielding_runs_prevented": 16,
        "outs_above_average": 8,
        "oaa_in_front": 1,
        "oaa_toward_3b_line": 0,
        "oaa_toward_1b_line": -1,
        "oaa_behind": 2,
        "oaa_vs_rhh": 7,
        "oaa_vs_lhh": 1,
    }
    for key in ("outs_above_average", "oaa_vs_rhh", "oaa_vs_lhh", "fielding_runs_prevented"):
        assert isinstance(out[key], int), key


def test_the_jump_columns_coerce_by_target() -> None:
    row = {
        "resp_fielder_id": "545361",
        "year": "2024",
        "n": "64",
        "n_outs": "50",
        "outs_above_average": "4",
        "rel_league_reaction_distance": "1.2",
        "rel_league_burst_distance": "0.4",
        "rel_league_routing_distance": "-0.3",
        "rel_league_bootup_distance": "1.3",
        "f_bootup_distance": "36.5",
    }
    out = sl.coerce_row(JUMP, row, 2024, None)
    assert out == {
        "player_id": 545361,
        "season": 2024,
        "n_plays": 64,
        "n_outs": 50,
        "outs_above_average": 4,
        "reaction_ft": 1.2,
        "burst_ft": 0.4,
        "route_ft": -0.3,
        "jump_ft": 1.3,
        "feet_covered": 36.5,
    }
    assert isinstance(out["n_plays"], int) and isinstance(out["outs_above_average"], int)


def test_a_blank_count_becomes_none_not_zero() -> None:
    """A zero would read as an average fielder, not as an absent measurement."""
    raw = {
        "player_id": "1",
        "primary_pos_formatted": "",
        "outs_above_average": "",
        "outs_above_average_rhh": "",
        "outs_above_average_lhh": None,
    }
    out = sl.coerce_row(OAA, raw, 2024, "SS")
    assert out is not None
    assert out["primary_position"] is None
    assert out["outs_above_average"] is None
    assert out["oaa_vs_rhh"] is None
    assert out["oaa_vs_lhh"] is None
    assert out["oaa_in_front"] is None  # absent from the CSV altogether


def test_the_integer_target_name_does_not_change_another_board_s_type() -> None:
    """``outs_above_average`` is a target on the two SIM-532 boards only. First
    base receiving stores ``total_oaa`` (a float), so it keeps its type."""
    owners = {
        b.name for b in BOARDS.values() if any(t == "outs_above_average" for _, t in b.columns)
    }
    assert owners == {"outs_above_average", "outfield_jump"}
    assert "total_oaa" not in sl._INT_TARGETS
    out = sl.coerce_row(BOARDS["first_base_receiving"], {"id": "1", "total_oaa": "2.5"}, 2024, None)
    assert out is not None and out["total_oaa"] == 2.5


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_the_oaa_upsert_keys_on_player_season_and_position() -> None:
    sql = sl.upsert_sql(OAA)
    assert "INSERT INTO raw.savant_outs_above_average" in sql
    assert "ON CONFLICT (player_id, season, position)" in sql
    assert "position = EXCLUDED.position" not in sql  # a key is not an update target
    assert "primary_position = EXCLUDED.primary_position" in sql
    assert "oaa_vs_lhh = EXCLUDED.oaa_vs_lhh" in sql


def test_the_jump_upsert_keys_on_player_and_season() -> None:
    sql = sl.upsert_sql(JUMP)
    assert "INSERT INTO raw.savant_outfield_jump" in sql
    assert "ON CONFLICT (player_id, season)" in sql
    assert "reaction_ft = EXCLUDED.reaction_ft" in sql


def test_the_target_columns_match_the_0027_tables() -> None:
    assert OAA.target_columns == (
        "player_id",
        "season",
        "position",
        "primary_position",
        "fielding_runs_prevented",
        "outs_above_average",
        "oaa_in_front",
        "oaa_toward_3b_line",
        "oaa_toward_1b_line",
        "oaa_behind",
        "oaa_vs_rhh",
        "oaa_vs_lhh",
    )
    assert JUMP.target_columns == (
        "player_id",
        "season",
        "n_plays",
        "n_outs",
        "outs_above_average",
        "reaction_ft",
        "burst_ft",
        "route_ft",
        "jump_ft",
        "feet_covered",
    )


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


def test_both_boards_are_in_the_fielding_group() -> None:
    assert "outs_above_average" in FIELDING_BOARDS
    assert "outfield_jump" in FIELDING_BOARDS
    names = {b.name for b in sl.resolve_boards(["fielding"])}
    assert {"outs_above_average", "outfield_jump"} <= names


def test_both_boards_are_pulled_at_the_full_roster_minimum() -> None:
    """Owner ruling 2026-09-16: the smallest minimum the endpoint honours. min=0
    is 551 fielders against 271 qualifiers and 212 outfielders against 100
    (2024)."""
    assert OAA.extra["min"] == "0"
    assert JUMP.extra["min"] == "0"
    assert OAA.extra.get("n") != "0" and JUMP.extra.get("n") != "0"


def test_the_oaa_board_pins_its_other_controls() -> None:
    assert OAA.extra == {
        "type": "Fielder",
        "min": "0",
        "split": "no",
        "range": "year",
        "viz": "hide",
    }
    assert OAA.table == "raw.savant_outs_above_average"
    assert JUMP.table == "raw.savant_outfield_jump"
    assert OAA.player_column == "player_id"
    assert JUMP.player_column == "resp_fielder_id"


# ---------------------------------------------------------------------------
# The migration and the reference DDL
# ---------------------------------------------------------------------------


def test_alembic_0027_creates_and_drops_the_two_raw_tables() -> None:
    text = ALEMBIC_0027.read_text(encoding="utf-8")
    assert 'revision = "0027"' in text
    assert 'down_revision = "0026"' in text
    assert "CREATE TABLE IF NOT EXISTS raw.savant_outs_above_average" in text
    assert "CREATE TABLE IF NOT EXISTS raw.savant_outfield_jump" in text
    assert "REFERENCES raw.players(player_id)" in text
    assert "PRIMARY KEY (player_id, season, position)" in text
    assert "PRIMARY KEY (player_id, season)" in text
    assert "def downgrade()" in text and "DROP TABLE IF EXISTS raw.{table}" in text
    assert "DROP INDEX IF EXISTS raw.idx_{table}_season" in text
    for col in OAA.target_columns + JUMP.target_columns:
        assert re.search(rf"^\s+{col}\s", text, re.MULTILINE), col


def test_the_postgres_reference_ddl_mirrors_the_two_tables() -> None:
    ddl = PG_SCHEMA_SQL.read_text(encoding="utf-8")
    assert "CREATE TABLE raw.savant_outs_above_average" in ddl
    assert "CREATE TABLE raw.savant_outfield_jump" in ddl
    assert "PRIMARY KEY (player_id, season, position)" in ddl
    assert "idx_savant_outs_above_average_season" in ddl
    assert "idx_savant_outfield_jump_season" in ddl
    for col in OAA.target_columns + JUMP.target_columns:
        assert re.search(rf"^\s+{col}\s", ddl, re.MULTILINE), col


# ---------------------------------------------------------------------------
# The finding stays honest
# ---------------------------------------------------------------------------


def _code_lines(path: Path) -> str:
    """The file without its comment lines, so a note ABOUT the split does not
    count as a read OF it."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


def test_the_hand_split_columns_are_stored_and_read_by_nothing() -> None:
    """Plan Finding 1: the fielder's outs above average against left-handers and
    against right-handers are two noisy halves of one skill (r 0.61 within a
    season in the outfield). Their difference repeats year to year at 0.04 to
    0.14 for outfielders and, within a position, only at shortstop (0.36 to
    0.38 in both the shift era and the ban era). A feature that does not repeat
    only adds noise to a draw weight, so the loader stores the two columns and
    neither the fielder model nor the profile builder names them. A later
    reader who wants the split at shortstop must first see these numbers."""
    assert {"oaa_vs_rhh", "oaa_vs_lhh"} <= set(OAA.target_columns)
    for path in (FIELDER_ENGINE, COMPUTOR):
        code = _code_lines(path)
        assert "oaa_vs_lhh" not in code, path.name
        assert "oaa_vs_rhh" not in code, path.name
