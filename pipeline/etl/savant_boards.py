"""
savant_boards.py
================
SIM-528 — the declarative registry of Baseball Savant leaderboards this
platform ingests.

WHY A REGISTRY
--------------
Eight Savant files feed three tickets. They differ in three ways that matter and
in no other way: the web address, how the season is named in the query, and
which columns we keep. Everything else — the browser-like user agent Savant
demands, the retry, the unknown-player guard, the upsert — is identical. So the
differences live here as data and the behaviour lives once in
``savant_loader.py``.

THE FOUR SEASON-PARAMETER STYLES (measured 2026-09-10, not assumed)
-------------------------------------------------------------------
Savant names the season four different ways, and **sending the wrong one fails
silently**: the board answers HTTP 200 with a well-formed CSV holding the
CURRENT season. Nothing errors. The rows are simply the wrong year.

    "year"    -> year=<s>                        arm strength, pop time
    "camel"   -> seasonStart=<s>&seasonEnd=<s>   the bat-tracking family, stance
    "snake"   -> season_start=<s>&season_end=<s> the run-value boards
    "bracket" -> season[]=<s>                    first base receiving

The fourth was found by the probe below, not by reading anything. First base
receiving accepts ``year``, ``seasonStart`` and ``season`` without complaint and
returns the current season for all three. Only ``season[]`` is honoured.

Two boards reject every other parameter. Fielding Run Value and Baserunning
answer only to a bare ``csv=true`` plus their season pair; adding ``year=``
returns zero rows or HTTP 500. Their ``extra`` dict is therefore empty on
purpose — do not "helpfully" add a team or minimum filter to them.

HOW THE LOADER PROVES THE SEASON WAS HONOURED
---------------------------------------------
Half these boards return no season column at all, so a per-row check is not
always possible. Every board therefore declares ``probe_season`` — a year with
no possible Statcast data. The loader asks for it once per board per run. An
honoured parameter returns zero rows; an ignored one returns the current
season's rows, and the loader fails loudly instead of writing the wrong year.

FULL ROSTER, NOT QUALIFIERS
---------------------------
Savant defaults to qualified players. ``minSwings=0`` on the bat-tracking family
and ``min=0`` elsewhere widen the result from roughly 215 batters to roughly
650. The ``extra`` dicts below set this. A row count near 215 in the log means a
minimum crept back in.

BATTING STANCE IS NOT UNDER /leaderboard/
-----------------------------------------
It lives at ``/visuals/batting-stance``. It is not linked as a leaderboard and
``/leaderboard/batting-stance`` is a 404.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BASE = "https://baseballsavant.mlb.com"

#: A season no Statcast board can hold data for. Used to prove the season
#: parameter is honoured (see the module docstring).
PROBE_SEASON = 1990


@dataclass(frozen=True, slots=True)
class SavantBoard:
    """One Savant CSV endpoint and how to turn it into a raw table row."""

    name: str
    url: str
    #: "year" | "camel" | "snake" — see the module docstring.
    season_style: str
    #: Target Postgres table, schema-qualified.
    table: str
    #: (savant CSV column, target column). The MLBAM player id and the season
    #: are handled separately, through ``player_column`` and ``season_column``.
    columns: tuple[tuple[str, str], ...]
    #: The CSV column holding the MLBAM player id.
    player_column: str
    #: The CSV column holding the season, when the board returns one. When this
    #: is set the loader asserts every row matches the season it asked for.
    season_column: str | None = None
    #: Query parameters beyond the season. Empty on the boards that reject them.
    extra: dict[str, str] = field(default_factory=dict)
    #: A third key column beyond (player_id, season), when the board emits more
    #: than one row per player-season.
    split_column: str | None = None
    #: When set, the loader pulls the board once per entry, adding the parameter
    #: and writing the label into ``split_column``. Used for the pitcher-hand
    #: splits (SIM-529: a switch hitter is two batters, not one).
    hand_splits: tuple[tuple[str, str], ...] = ()
    probe_season: int = PROBE_SEASON

    def season_params(self, season: int) -> dict[str, str]:
        s = str(season)
        if self.season_style == "year":
            return {"year": s}
        if self.season_style == "camel":
            return {"seasonStart": s, "seasonEnd": s}
        if self.season_style == "snake":
            return {"season_start": s, "season_end": s}
        if self.season_style == "bracket":
            return {"season[]": s}
        raise ValueError(f"{self.name}: unknown season style {self.season_style!r}")

    @property
    def target_columns(self) -> tuple[str, ...]:
        cols = ["player_id", "season"]
        if self.split_column:
            cols.append(self.split_column)
        cols += [t for _, t in self.columns]
        return tuple(cols)


# --- the pitcher-hand splits ------------------------------------------------
# SIM-529, owner ruling 2026-09-10: a switch hitter's two sides are separate
# rows, not one collapsed row. The swing boards report a single row per batter
# labelled with his majority side, so the split has to come from the query. A
# batter's side is decided by the pitcher's hand, so filtering on the pitcher's
# hand IS the batting-side split: vs a right-handed pitcher a switch hitter bats
# left, and vs a left-handed pitcher he bats right.
#
#   label   parameter          what it holds
#   all     (none)             every swing, both sides for a switch hitter
#   vs_l    pitchHand=L        his swings against left-handed pitchers
#   vs_r    pitchHand=R        his swings against right-handed pitchers
_HAND_SPLITS = (("all", ""), ("vs_l", "L"), ("vs_r", "R"))


BOARDS: dict[str, SavantBoard] = {
    # -- SIM-529: the physical swing and stance boards ----------------------
    "bat_tracking": SavantBoard(
        name="bat_tracking",
        url=f"{BASE}/leaderboard/bat-tracking",
        season_style="camel",
        table="raw.savant_bat_tracking",
        player_column="id",
        split_column="split",
        hand_splits=_HAND_SPLITS,
        extra={
            "type": "batter",
            "minSwings": "0",
            "minGroupSwings": "1",
            "gameType": "Regular",
        },
        columns=(
            ("avg_bat_speed", "avg_bat_speed"),
            ("swing_length", "swing_length"),
            ("swings_competitive", "competitive_swings"),
        ),
    ),
    "swing_path": SavantBoard(
        name="swing_path",
        url=f"{BASE}/leaderboard/bat-tracking/swing-path-attack-angle",
        season_style="camel",
        table="raw.savant_swing_path",
        player_column="id",
        split_column="split",
        hand_splits=_HAND_SPLITS,
        extra={
            "type": "batter",
            "minSwings": "0",
            "minGroupSwings": "1",
            "gameType": "Regular",
        },
        columns=(
            ("side", "bat_side"),
            ("swing_tilt", "swing_tilt"),
            ("attack_angle", "attack_angle"),
            ("attack_direction", "attack_direction"),
            ("ideal_attack_angle_rate", "ideal_attack_angle_rate"),
            ("avg_intercept_y_vs_plate", "intercept_y_vs_plate"),
            ("avg_intercept_y_vs_batter", "intercept_y_vs_batter"),
            ("competitive_swings", "competitive_swings"),
        ),
    ),
    # The stance board is the one place Savant itself emits a row per batting
    # side, so it needs no hand split — ``bat_side`` IS the key.
    "batting_stance": SavantBoard(
        name="batting_stance",
        url=f"{BASE}/visuals/batting-stance",
        season_style="camel",
        table="raw.savant_batting_stance",
        player_column="id",
        split_column="bat_side",
        extra={
            "type": "batter",
            "minSwings": "0",
            "minGroupSwings": "1",
            "minContact": "0",
            "gameType": "Regular",
        },
        columns=(
            ("avg_foot_sep", "foot_sep"),
            ("avg_stance_angle", "stance_angle"),
            ("avg_batter_y_position", "batter_y_position"),
            ("avg_batter_x_position", "batter_x_position"),
            ("avg_intercept_y_vs_plate", "intercept_y_vs_plate"),
            ("avg_intercept_y_vs_batter", "intercept_y_vs_batter"),
        ),
    ),
    # -- SIM-530: the boards that fill the empty measurement blocks ----------
    "arm_strength": SavantBoard(
        name="arm_strength",
        url=f"{BASE}/leaderboard/arm-strength",
        season_style="year",
        table="raw.savant_arm_strength",
        player_column="player_id",
        extra={"type": "player", "minThrows": "0"},
        columns=(
            ("total_throws", "total_throws"),
            ("max_arm_strength", "max_arm_strength"),
            ("arm_overall", "arm_overall"),
            ("arm_inf", "arm_inf"),
            ("arm_of", "arm_of"),
            ("arm_1b", "arm_1b"),
            ("arm_2b", "arm_2b"),
            ("arm_3b", "arm_3b"),
            ("arm_ss", "arm_ss"),
            ("arm_lf", "arm_lf"),
            ("arm_cf", "arm_cf"),
            ("arm_rf", "arm_rf"),
        ),
    ),
    # Baserunning answers ONLY to a bare csv=true plus the season pair. The
    # empty ``extra`` is deliberate — see the module docstring.
    "baserunning": SavantBoard(
        name="baserunning",
        url=f"{BASE}/leaderboard/baserunning",
        season_style="snake",
        table="raw.savant_baserunning",
        player_column="entity_id",
        season_column="year",
        columns=(
            ("fielder_runs", "fielder_runs"),
            ("fielder_runs_advances", "fielder_runs_advances"),
            ("fielder_runs_thrown_out", "fielder_runs_thrown_out"),
            ("fielder_runs_hold", "fielder_runs_hold"),
            ("runner_runs", "runner_runs"),
            ("n_opp_xb", "n_opp_xb"),
            ("n_att_xb", "n_att_xb"),
            ("rate_att_xb", "rate_att_xb"),
            ("est_rate_att_generic_fielder", "est_rate_att_generic_fielder"),
            ("est_rate_att_generic_runner", "est_rate_att_generic_runner"),
            ("n_out", "n_out"),
            ("n_safe", "n_safe"),
        ),
    ),
    "poptime": SavantBoard(
        name="poptime",
        url=f"{BASE}/leaderboard/poptime",
        season_style="year",
        table="raw.savant_poptime",
        player_column="entity_id",
        extra={"min2b": "0", "min3b": "0"},
        columns=(
            ("maxeff_arm_2b_3b_sba", "arm_strength"),
            ("exchange_2b_3b_sba", "exchange_time"),
            ("pop_2b_sba", "pop_time_2b"),
            ("pop_2b_sba_count", "pop_time_2b_count"),
            ("pop_3b_sba", "pop_time_3b"),
        ),
    ),
    "catcher_throwing": SavantBoard(
        name="catcher_throwing",
        url=f"{BASE}/leaderboard/catcher-throwing",
        season_style="snake",
        table="raw.savant_catcher_throwing",
        player_column="player_id",
        season_column="start_year",
        columns=(
            ("arm_strength", "arm_strength"),
            ("pop_time", "pop_time"),
            ("exchange_time", "exchange_time"),
            ("est_cs_pct", "est_cs_pct"),
            ("cs_aa_per_throw", "cs_aa_per_throw"),
            ("sb_attempts", "sb_attempts"),
            ("n_cs", "n_cs"),
        ),
    ),
    "first_base_receiving": SavantBoard(
        name="first_base_receiving",
        url=f"{BASE}/leaderboard/first-base-scoops-receiving",
        # The only board on the "bracket" style. It silently ignores every other
        # spelling and serves the current season — the probe caught it.
        season_style="bracket",
        table="raw.savant_first_base_receiving",
        player_column="id",
        season_column="year",
        columns=(
            ("height_in_inches", "height_in_inches"),
            ("n_plays", "n_plays"),
            ("n_outs", "n_outs"),
            ("total_oaa", "total_oaa"),
            ("n_scoop", "n_scoop"),
            ("outs_scoop", "outs_scoop"),
            ("oaa_scoop", "oaa_scoop"),
        ),
    ),
}

#: The boards SIM-529 (batter swing features) consumes.
BATTER_BOARDS = ("bat_tracking", "swing_path", "batting_stance")
#: The boards SIM-530 (the empty measurement blocks) consumes.
FIELDING_BOARDS = (
    "arm_strength",
    "baserunning",
    "poptime",
    "catcher_throwing",
    "first_base_receiving",
)

__all__ = ["BATTER_BOARDS", "BOARDS", "FIELDING_BOARDS", "PROBE_SEASON", "SavantBoard"]
