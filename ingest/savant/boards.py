"""The registry of the Baseball Savant leaderboards the platform loads, with each board's traps.

The boards differ in three ways only: the web address, how the query names the
season, and the columns we keep. This module holds those differences as data.
``ingest/savant/load_leaderboards.py`` holds the one behaviour: the fetch, the
retry, the unknown-player guard and the upsert.

THE FIVE SEASON-PARAMETER STYLES
--------------------------------
Savant names the season in five ways. The wrong name fails silently: the board
answers HTTP 200 with a valid CSV of the CURRENT season.

    "year"      -> year=<s>                        arm strength, pop time, outfield jump
    "camel"     -> seasonStart=<s>&seasonEnd=<s>   the bat-tracking family, stance
    "snake"     -> season_start=<s>&season_end=<s> the run-value boards
    "bracket"   -> season[]=<s>                    first base receiving
    "startyear" -> startYear=<s>&endYear=<s>       outs above average

First base receiving accepts ``year``, ``seasonStart`` and ``season`` with no
error, and serves the current season for each. It honours only ``season[]``.

HOW THE LOADER PROVES THE SEASON WAS HONOURED
---------------------------------------------
Half these boards return no season column, so a per-row check cannot always run.
Once per board per run, the loader asks for ``PROBE_SEASON``, a year with no
Statcast data. An honoured parameter returns zero rows. An ignored one returns
the current season, and the loader fails.

THE OUTS-ABOVE-AVERAGE BOARD
----------------------------
1. Its ``year`` column is BLANK on every row. The per-row season check cannot
   run (``season_column=None``), so the probe is the only guard.
2. Its figure is the fielder's AT the pulled position, which differs from his
   all-positions figure. The loader pulls the board once per position (``pos=3``
   … ``pos=9``) and writes ``position`` from the query. The CSV's
   ``primary_pos_formatted`` is the player's PRIMARY position, so the loader
   stores it as ``primary_position``.

The split by the batter's hand (``oaa_vs_rhh``, ``oaa_vs_lhh``) is stored and
read by nothing: the left-minus-right gap barely repeats from year to year.

THE BASERUNNING BOARD
---------------------
The board IGNORES ``year=`` and serves the current season under it. It also
ignores ``min=``. It honours ``n=`` (the minimum number of chances) and
``type=``, which picks one of two views:

* ``type=Run`` is the RUNNER's side: his extra-base chances, his attempts and
  the runs he earned. The board serves this view when ``type`` is absent.
* ``type=Fld`` is the FIELDER's side: the chances runners had against him, the
  runners he threw out and his arm run value. A designated hitter is absent.

The spelling ``type=fielder`` is silently ignored and serves the runner view.
The ``baserunning`` entry pins ``type=Run``. The fielder view needs its own pull.

Fielding Run Value (not loaded) rejects every parameter except its season pair:
``year=`` gives zero rows or HTTP 500.

FULL ROSTER, NOT QUALIFIERS
---------------------------
Savant defaults to qualified players. Every board is pulled at the smallest
minimum its endpoint honours:

    bat-tracking family, stance   minSwings=0         min 1 swing
    basestealing                  n=1                 min 1 chance
    pitcher running game          n=1                 min 1 chance
    baserunning                   n=1                 min 1 chance
    catcher throwing              n=1                 min 1 attempt
    first base receiving          min=1               min 1 play
    pop time                      min2b=0 & min3b=0
    arm strength                  (none honoured)     Savant's floor is 50 throws
    sprint speed (own loader)     min=0               Savant's floor is 5 runs
    outs above average            min=0               min 1 chance
    outfield jump                 min=0               min 1 play (min=1 is the same)

The run-value boards IGNORE ``n=0``: they read it as "not set" and serve the
qualified default. A row count in the log near the qualified count means a
minimum crept back in.

BATTING STANCE IS NOT UNDER /leaderboard/
-----------------------------------------
The stance board lives at ``/visuals/batting-stance``.
``/leaderboard/batting-stance`` is a 404.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BASE = "https://baseballsavant.mlb.com"

#: A season no Statcast board can hold data for. The probe asks for it to prove
#: a board honours its season parameter.
PROBE_SEASON = 1990


@dataclass(frozen=True, slots=True)
class SavantBoard:
    """One Savant CSV endpoint and how to turn its rows into raw table rows."""

    name: str
    url: str
    #: "year" | "camel" | "snake" | "bracket" | "startyear" (the module docstring).
    season_style: str
    #: The target Postgres table, with its schema.
    table: str
    #: (Savant CSV column, target column). The player id and the season are
    #: handled through ``player_column`` and ``season_column``.
    columns: tuple[tuple[str, str], ...]
    #: The CSV column that holds the MLBAM player id.
    player_column: str
    #: The CSV column that holds the season, when the board returns one. The
    #: loader then checks that every row carries the season it asked for.
    season_column: str | None = None
    #: The query parameters beyond the season. Leave it empty for a board that
    #: rejects extra parameters.
    extra: dict[str, str] = field(default_factory=dict)
    #: A third key column after (player_id, season), for a board with more than
    #: one row per player-season.
    split_column: str | None = None
    #: The query parameter a split pull varies: ``pitchHand`` for the
    #: pitcher-hand split, ``pos`` for the per-position pull.
    split_param: str = "pitchHand"
    #: The (label, value) pairs of a split pull. The loader pulls the board once
    #: per pair. It sends ``split_param=<value>`` (an empty value sends nothing)
    #: and writes the label into ``split_column``.
    splits: tuple[tuple[str, str], ...] = ()
    #: True when the board accepts ``dateStart`` / ``dateEnd``, so the loader can
    #: pull it as of a date. The fielding and running boards have no date control.
    supports_date_range: bool = False
    #: The target column for the cutoff date of an as-of pull.
    asof_column: str | None = None

    def season_params(self, season: int) -> dict[str, str]:
        """Return the query parameters that name ``season`` in this board's style."""
        s = str(season)
        if self.season_style == "year":
            return {"year": s}
        if self.season_style == "camel":
            return {"seasonStart": s, "seasonEnd": s}
        if self.season_style == "snake":
            return {"season_start": s, "season_end": s}
        if self.season_style == "bracket":
            return {"season[]": s}
        if self.season_style == "startyear":
            return {"startYear": s, "endYear": s}
        raise ValueError(f"{self.name}: unknown season style {self.season_style!r}")

    @property
    def target_columns(self) -> tuple[str, ...]:
        """The target table's columns in upsert order: the key, then the board's columns."""
        cols = ["player_id", "season"]
        if self.asof_column:
            cols.append(self.asof_column)
        if self.split_column:
            cols.append(self.split_column)
        cols += [t for _, t in self.columns]
        return tuple(cols)


# --- the pitcher-hand splits ------------------------------------------------
# A switch hitter's two sides are separate rows. The swing boards report one
# row per batter, labelled with his majority side, so the split must come from
# the query. The pitcher's hand decides the batter's side: against a
# right-handed pitcher a switch hitter bats left. So a filter on the pitcher's
# hand IS the batting-side split.
#
#   label   parameter          what it holds
#   all     (none)             every swing, both sides for a switch hitter
#   vs_l    pitchHand=L        his swings against left-handed pitchers
#   vs_r    pitchHand=R        his swings against right-handed pitchers
_HAND_SPLITS = (("all", ""), ("vs_l", "L"), ("vs_r", "R"))

# --- the per-position pull ------------------------------------------------
# The outs-above-average board is pulled once per position, because its figure
# is the fielder's AT that position. The label is the position the fielder
# profile keys on. The value is Savant's ``pos`` code.
#
#   label   parameter   position
#   1B      pos=3       first base
#   2B      pos=4       second base
#   3B      pos=5       third base
#   SS      pos=6       shortstop
#   LF      pos=7       left field
#   CF      pos=8       center field
#   RF      pos=9       right field
_POSITION_SPLITS = (
    ("1B", "3"),
    ("2B", "4"),
    ("3B", "5"),
    ("SS", "6"),
    ("LF", "7"),
    ("CF", "8"),
    ("RF", "9"),
)


BOARDS: dict[str, SavantBoard] = {
    # -- the swing and stance boards ------------------------------------------
    "bat_tracking": SavantBoard(
        name="bat_tracking",
        url=f"{BASE}/leaderboard/bat-tracking",
        season_style="camel",
        table="raw.savant_bat_tracking",
        player_column="id",
        split_column="split",
        splits=_HAND_SPLITS,
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
        splits=_HAND_SPLITS,
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
    # The stance board emits a row per batting side itself, so it needs no hand
    # split: ``bat_side`` IS the key.
    "batting_stance": SavantBoard(
        name="batting_stance",
        url=f"{BASE}/visuals/batting-stance",
        season_style="camel",
        table="raw.savant_batting_stance",
        player_column="id",
        split_column="bat_side",
        # Stance comes from pose tracking, so the pitch-level export lacks it.
        # The board takes a date range: a point-in-time stance is one row per cutoff.
        supports_date_range=True,
        asof_column="asof_date",
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
    # -- the arm and baserunning boards ---------------------------------------
    "arm_strength": SavantBoard(
        name="arm_strength",
        url=f"{BASE}/leaderboard/arm-strength",
        season_style="year",
        table="raw.savant_arm_strength",
        player_column="player_id",
        # ``minThrows`` is the page's own control. Its smallest option is 50, and
        # the CSV honours nothing below it.
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
    # The RUNNER view, pinned as ``type=Run``. ``n=1`` is every runner with one
    # extra-base chance. The board ignores ``year=`` and ``type=fielder``; the
    # module docstring has its traps.
    "baserunning": SavantBoard(
        name="baserunning",
        url=f"{BASE}/leaderboard/baserunning",
        season_style="snake",
        table="raw.savant_baserunning",
        player_column="entity_id",
        season_column="year",
        extra={"n": "1", "type": "Run"},
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
    # -- the two running-game boards ------------------------------------------
    # One shape, two sides. Both honour the season pair and carry the season on
    # every row (``start_year``). ``n`` is the minimum number of chances: ``n=1``
    # is every player with one. The boards IGNORE ``n=0``: they read it as "not
    # set" and serve the qualified default. So the registry never sends it, and
    # a test forbids it. The ``*_sbx`` leads (attempted pitches only) are stored
    # and read by nothing: they repeat poorly from year to year.
    "basestealing": SavantBoard(
        name="basestealing",
        url=f"{BASE}/leaderboard/basestealing-run-value",
        season_style="snake",
        table="raw.savant_basestealing",
        player_column="player_id",
        season_column="start_year",
        extra={"n": "1"},
        columns=(
            ("n_init", "n_init"),
            ("rate_sbx", "rate_sbx"),
            ("n_sb", "n_sb"),
            ("n_cs", "n_cs"),
            ("n_pk", "n_pk"),
            ("n_bk", "n_bk"),
            ("runs_stolen_on_running_act", "runs_stolen_on_running_act"),
            ("r_primary_lead", "r_primary_lead"),
            ("r_secondary_lead", "r_secondary_lead"),
            ("r_sec_minus_prim_lead", "r_sec_minus_prim_lead"),
            ("r_primary_lead_sbx", "r_primary_lead_sbx"),
            ("r_secondary_lead_sbx", "r_secondary_lead_sbx"),
            ("r_sec_minus_prim_lead_sbx", "r_sec_minus_prim_lead_sbx"),
        ),
    ),
    "pitcher_running_game": SavantBoard(
        name="pitcher_running_game",
        url=f"{BASE}/leaderboard/pitcher-running-game",
        season_style="snake",
        table="raw.savant_pitcher_running_game",
        player_column="player_id",
        season_column="start_year",
        extra={"n": "1"},
        columns=(
            ("n_init", "n_init"),
            ("rate_sbx", "rate_sbx"),
            ("n_sb", "n_sb"),
            ("n_cs", "n_cs"),
            ("n_pk", "n_pk"),
            ("n_bk", "n_bk"),
            ("runs_prevented_on_running_attr", "runs_prevented_on_running_attr"),
            ("n_pitcher_cs_aa", "n_pitcher_cs_aa"),
            ("r_primary_lead", "r_primary_lead"),
            ("r_secondary_lead", "r_secondary_lead"),
            ("r_sec_minus_prim_lead", "r_sec_minus_prim_lead"),
            ("r_primary_lead_sbx", "r_primary_lead_sbx"),
            ("r_secondary_lead_sbx", "r_secondary_lead_sbx"),
            ("r_sec_minus_prim_lead_sbx", "r_sec_minus_prim_lead_sbx"),
        ),
    ),
    # -- the catcher and first-base boards -----------------------------------
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
        # Every catcher with one steal attempt against him.
        extra={"n": "1"},
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
        # The one "bracket" board. It silently ignores every other spelling and
        # serves the current season.
        season_style="bracket",
        table="raw.savant_first_base_receiving",
        player_column="id",
        season_column="year",
        # Every first baseman with one play.
        extra={"min": "1"},
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
    # -- the outs above average per position, and the outfield jump -----------
    # One pull per position (``pos=3`` … ``pos=9``): the figure is the
    # fielder's AT that position, so the loader writes ``position`` from the
    # query. The ``year`` column is BLANK on every row, so the probe is the only
    # season guard. ``min=0`` is every fielder with one chance. The three
    # ``*_formatted`` success rates (percent strings) are not stored.
    "outs_above_average": SavantBoard(
        name="outs_above_average",
        url=f"{BASE}/leaderboard/outs_above_average",
        season_style="startyear",
        table="raw.savant_outs_above_average",
        player_column="player_id",
        season_column=None,
        split_column="position",
        split_param="pos",
        splits=_POSITION_SPLITS,
        extra={"type": "Fielder", "min": "0", "split": "no", "range": "year", "viz": "hide"},
        columns=(
            ("primary_pos_formatted", "primary_position"),
            ("fielding_runs_prevented", "fielding_runs_prevented"),
            ("outs_above_average", "outs_above_average"),
            ("outs_above_average_infront", "oaa_in_front"),
            ("outs_above_average_lateral_toward3bline", "oaa_toward_3b_line"),
            ("outs_above_average_lateral_toward1bline", "oaa_toward_1b_line"),
            ("outs_above_average_behind", "oaa_behind"),
            ("outs_above_average_rhh", "oaa_vs_rhh"),
            ("outs_above_average_lhh", "oaa_vs_lhh"),
        ),
    ),
    # Per player, not per position: an outfielder's jump is the same at any
    # outfield spot. The board carries ``year`` on every row, so the per-row
    # check runs. ``min=0`` is every outfielder with one play. The four
    # ``rel_league_*`` columns are feet against the league average.
    # ``outs_per_play`` is not stored.
    "outfield_jump": SavantBoard(
        name="outfield_jump",
        url=f"{BASE}/leaderboard/outfield_jump",
        season_style="year",
        table="raw.savant_outfield_jump",
        player_column="resp_fielder_id",
        season_column="year",
        extra={"min": "0"},
        columns=(
            ("n", "n_plays"),
            ("n_outs", "n_outs"),
            ("outs_above_average", "outs_above_average"),
            ("rel_league_reaction_distance", "reaction_ft"),
            ("rel_league_burst_distance", "burst_ft"),
            ("rel_league_routing_distance", "route_ft"),
            ("rel_league_bootup_distance", "jump_ft"),
            ("f_bootup_distance", "feet_covered"),
        ),
    ),
}

#: The boards the batter's swing features read.
BATTER_BOARDS = ("bat_tracking", "swing_path", "batting_stance")
#: The boards that fill the profiles' fielding, throwing and baserunning
#: measurements. The fielder model's range groups read the last two.
FIELDING_BOARDS = (
    "arm_strength",
    "baserunning",
    "poptime",
    "catcher_throwing",
    "first_base_receiving",
    "outs_above_average",
    "outfield_jump",
)
#: The boards the steal and baserunning models read: the runner's lead and jump,
#: the pitcher's lead and jump allowed, and the extra-base attempt rate above
#: expectation.
RUNNING_BOARDS = ("basestealing", "pitcher_running_game", "baserunning")

__all__ = [
    "BATTER_BOARDS",
    "BOARDS",
    "FIELDING_BOARDS",
    "PROBE_SEASON",
    "RUNNING_BOARDS",
    "SavantBoard",
]
