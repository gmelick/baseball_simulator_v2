"""
savant_boards.py
================
SIM-528 — the declarative registry of Baseball Savant leaderboards this
platform ingests.

WHY A REGISTRY
--------------
Twelve Savant files feed four tickets. They differ in three ways that matter and
in no other way: the web address, how the season is named in the query, and
which columns we keep. Everything else — the browser-like user agent Savant
demands, the retry, the unknown-player guard, the upsert — is identical. So the
differences live here as data and the behaviour lives once in
``savant_loader.py``.

THE FIVE SEASON-PARAMETER STYLES (measured 2026-09-10 and 2026-09-17, not assumed)
----------------------------------------------------------------------------------
Savant names the season five different ways, and **sending the wrong one fails
silently**: the board answers HTTP 200 with a well-formed CSV holding the
CURRENT season. Nothing errors. The rows are simply the wrong year.

    "year"      -> year=<s>                        arm strength, pop time, outfield jump
    "camel"     -> seasonStart=<s>&seasonEnd=<s>   the bat-tracking family, stance
    "snake"     -> season_start=<s>&season_end=<s> the run-value boards
    "bracket"   -> season[]=<s>                    first base receiving
    "startyear" -> startYear=<s>&endYear=<s>       outs above average (SIM-532)

The fourth was found by the probe below, not by reading anything. First base
receiving accepts ``year``, ``seasonStart`` and ``season`` without complaint and
returns the current season for all three. Only ``season[]`` is honoured. The
fifth was probed on 2026-09-17: the outs-above-average board answers
``startYear=1990&endYear=1990`` with zero rows, so the probe passes.

THE OUTS-ABOVE-AVERAGE BOARD: A BLANK YEAR AND A PULL PER POSITION (SIM-532)
---------------------------------------------------------------------------
Two traps, both measured on 2026-09-17.

1. **Its ``year`` column is BLANK on every row.** The loader's per-row season
   check cannot run on it (``season_column=None``). The probe is the only
   guard, and it runs once per board per load.
2. **The figure is the fielder's AT the pulled position.** The board is pulled
   once per position (``pos=3`` … ``pos=9``), and 92 of the 147 center-field
   rows of 2024 differ from the same player's all-positions figure. The CSV's
   ``primary_pos_formatted`` column is the player's PRIMARY position, not the
   pulled one, so the loader writes ``position`` from the query and keeps the
   board's label in ``primary_position``.

The board also carries the split by the batter's hand (``oaa_vs_rhh``,
``oaa_vs_lhh``). Those two columns are stored for the record and read by
nothing: the left-minus-right gap repeats year to year at 0.04 to 0.14 for
outfielders and, within a position, only at shortstop (plan Finding 1).

Fielding Run Value (not loaded) rejects every parameter but its season pair:
adding ``year=`` returns zero rows or HTTP 500. Baserunning was believed to
as well; it does take ``n=`` (measured 2026-09-16: ``n=1`` widens 2024 from
305 runners to 623), and it IGNORES ``year=`` — it serves the CURRENT season
under it (633 rows stamped 2026 for ``year=2024``, measured the same day). That
is the silent wrong-year failure the probe and ``check_row_seasons`` exist for.

THE BASERUNNING BOARD HAS TWO VIEWS (measured 2026-09-16)
--------------------------------------------------------
``type=Run`` is the RUNNER's side — his own extra-base chances, attempts and
the runs he earned — and it is what the board serves when ``type`` is absent.
``type=Fld`` is the FIELDER's side — the chances runners had against him, the
attempts, the runners he threw out, the generic-fielder expectation and his arm
run value; a designated hitter appears in the runner view and not there. The
spelling ``type=fielder`` is silently IGNORED (it serves the runner view), which
is how the 2026-09-10 audit concluded the board had no fielder view and how the
SIM-530 join came to read the runner view as the fielder's (SIM-550). The
``baserunning`` entry below pins ``type=Run``; the fielder view is a separate
pull when a ticket needs it.

HOW THE LOADER PROVES THE SEASON WAS HONOURED
---------------------------------------------
Half these boards return no season column at all, so a per-row check is not
always possible. Every board therefore declares ``probe_season`` — a year with
no possible Statcast data. The loader asks for it once per board per run. An
honoured parameter returns zero rows; an ignored one returns the current
season's rows, and the loader fails loudly instead of writing the wrong year.

FULL ROSTER, NOT QUALIFIERS (owner ruling 2026-09-16)
------------------------------------------------------
Savant defaults to qualified players. Every board is pulled at the SMALLEST
minimum its endpoint honours, measured live on 2024 (2026-09-16):

    bat-tracking family, stance   minSwings=0        215 -> 650 batters (min 1 swing)
    basestealing (SIM-531)        n=1                432 -> 638 runners (min 1 chance)
    pitcher running game (SIM-531) n=1               496 -> 849 pitchers (min 1 chance)
    baserunning                   n=1                305 -> 623 runners (min 1 chance)
    catcher throwing              n=1                 66 ->  94 catchers (min 1 attempt)
    first base receiving          min=1               42 -> 152 first basemen (min 1 play)
    pop time                      min2b=0 & min3b=0   83 -> 100 catchers
    arm strength                  (none honoured)    388 either way: Savant's own floor is
                                                     50 throws — its smallest dropdown option
    sprint speed (its own loader) min=0              566 -> 606 runners; 5 runs is Savant's floor
    outs above average (SIM-532)  min=0              271 -> 551 fielders (2024, all positions)
    outfield jump (SIM-532)       min=0              100 -> 212 outfielders (2024; min=1 the same)

``n=0`` is IGNORED by the run-value boards (it reads as "not set" and serves the
qualified default) — the smallest honoured value is ``n=1``. A row count near
the qualified figure in the log means a minimum crept back in.

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
    #: "year" | "camel" | "snake" | "bracket" | "startyear" — see the module docstring.
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
    #: The query parameter a split pull varies. The pitcher-hand split (SIM-529)
    #: is ``pitchHand``; the per-position outs-above-average pull (SIM-532) is
    #: ``pos``.
    split_param: str = "pitchHand"
    #: The (label, value) pairs of a split pull. When set, the loader pulls the
    #: board once per entry, sending ``split_param=<value>`` (an empty value
    #: sends nothing) and writing the label into ``split_column``. Used for the
    #: pitcher-hand splits (SIM-529: a switch hitter is two batters, not one)
    #: and the per-position pull (SIM-532: the figure is AT that position).
    splits: tuple[tuple[str, str], ...] = ()
    probe_season: int = PROBE_SEASON
    #: SIM-534: the board accepts ``dateStart`` / ``dateEnd``, so it can be
    #: pulled "as of" a date instead of as a whole season. Only the bat-tracking
    #: family does; the fielding and running boards have no date control at all.
    supports_date_range: bool = False
    #: Target column holding the cutoff, when the board is pulled as of a date.
    asof_column: str | None = None

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
        if self.season_style == "startyear":
            # SIM-532: the outs-above-average board. Probed 2026-09-17: 1990
            # returns zero rows, so the pair is honoured.
            return {"startYear": s, "endYear": s}
        raise ValueError(f"{self.name}: unknown season style {self.season_style!r}")

    @property
    def hand_splits(self) -> tuple[tuple[str, str], ...]:
        """The old name of ``splits`` (SIM-529). Read-only; kept one release."""
        return self.splits

    @property
    def target_columns(self) -> tuple[str, ...]:
        cols = ["player_id", "season"]
        if self.asof_column:
            cols.append(self.asof_column)
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

# --- the per-position pull ------------------------------------------------
# SIM-532: the outs-above-average board is pulled once per position, because
# the figure is the fielder's AT that position (92 of the 147 center-field rows
# of 2024 differ from the same player's all-positions figure). The label is the
# position the fielder profile keys on; the value is Savant's ``pos`` code.
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
    # -- SIM-529: the physical swing and stance boards ----------------------
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
    # The stance board is the one place Savant itself emits a row per batting
    # side, so it needs no hand split — ``bat_side`` IS the key.
    "batting_stance": SavantBoard(
        name="batting_stance",
        url=f"{BASE}/visuals/batting-stance",
        season_style="camel",
        table="raw.savant_batting_stance",
        player_column="id",
        split_column="bat_side",
        # SIM-534: stance is the ONE batter measurement absent from the
        # pitch-level export — it comes from pose tracking. The board does take a
        # date range, so a point-in-time stance means one row per cutoff.
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
    # -- SIM-530: the boards that fill the empty measurement blocks ----------
    "arm_strength": SavantBoard(
        name="arm_strength",
        url=f"{BASE}/leaderboard/arm-strength",
        season_style="year",
        table="raw.savant_arm_strength",
        player_column="player_id",
        # minThrows is the page's own control, but its smallest option is 50 and
        # the CSV honours nothing below it (388 rows for 0, 1, 5 and unset alike).
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
    # Baserunning ignores ``year=`` (it serves the current season) and ``min=``,
    # but it honours ``n=`` (the minimum number of chances: n=1 is every runner
    # with one extra-base chance, 623 in 2024 against 305 qualifiers) and
    # ``type=``. This entry is the RUNNER view, pinned as ``type=Run`` (the
    # board's default when absent). The FIELDER view is ``type=Fld`` — a separate
    # pull (SIM-550); ``type=fielder`` is silently ignored and serves the runner view.
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
    # -- SIM-531: the two running-game boards ---------------------------------
    # One shape, two sides. Both honour the season pair (1990 returns zero rows)
    # and carry the season on every row (start_year). ``n`` is the minimum
    # number of chances: n=1 is every player with one (2024: 638 runners against
    # 432 qualifiers; 849 pitchers against 496). ``n=0`` is IGNORED — it reads
    # as "not set" and serves the qualified default — so the registry never
    # sends it (a test forbids it). The ``*_sbx`` leads (attempted pitches only)
    # are stored, not read: they repeat year to year at 0.29-0.32.
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
        # every catcher with one steal attempt against (94 vs 66 qualifiers, 2024)
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
        # The only board on the "bracket" style. It silently ignores every other
        # spelling and serves the current season — the probe caught it.
        season_style="bracket",
        table="raw.savant_first_base_receiving",
        player_column="id",
        season_column="year",
        # every first baseman with one play (152 vs 42 qualifiers, 2024)
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
    # -- SIM-532: Savant's per-position outs above average and the outfield jump --
    # One pull per position (``pos=3`` … ``pos=9``): the figure is the fielder's
    # AT that position, and the loader writes ``position`` from the query. The
    # board's ``year`` column is BLANK on every row, so there is no per-row
    # season check; the probe (1990 -> 0 rows, measured 2026-09-17) is the only
    # guard. ``min=0`` is every fielder with one chance (551 against 271
    # qualifiers, 2024). The three ``*_formatted`` success rates (percent
    # strings) are not stored. The hand split (``oaa_vs_rhh``, ``oaa_vs_lhh``)
    # is stored and read by nothing (the module docstring).
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
    # check runs. ``min=0`` is every outfielder with one play (212 against 100
    # qualifiers, 2024; ``min=1`` returns the same). The four ``rel_league_*``
    # columns are feet against the league average; ``outs_per_play`` is not
    # stored.
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

#: The boards SIM-529 (batter swing features) consumes.
BATTER_BOARDS = ("bat_tracking", "swing_path", "batting_stance")
#: The boards SIM-530 (the empty measurement blocks) consumes, plus the two
#: SIM-532 boards (Savant's per-position outs above average and the outfield
#: jump, both read by the fielder model's range groups).
FIELDING_BOARDS = (
    "arm_strength",
    "baserunning",
    "poptime",
    "catcher_throwing",
    "first_base_receiving",
    "outs_above_average",
    "outfield_jump",
)
#: The boards SIM-531 (lead distance in the steal and baserunning models)
#: consumes: the runner's lead and jump, the pitcher's lead and jump allowed,
#: and the extra-base attempt rate above expectation.
RUNNING_BOARDS = ("basestealing", "pitcher_running_game", "baserunning")

__all__ = [
    "BATTER_BOARDS",
    "BOARDS",
    "FIELDING_BOARDS",
    "PROBE_SEASON",
    "RUNNING_BOARDS",
    "SavantBoard",
]
